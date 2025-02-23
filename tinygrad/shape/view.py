from __future__ import annotations
import functools
from typing import Tuple, List, Optional, NamedTuple, Union
from tinygrad.helpers import prod
from tinygrad.shape.symbolic import Variable, Node, is_sym_int

@functools.lru_cache(maxsize=None)
def to_shape_strides(shape:Tuple[int, ...], strides:Tuple[int, ...]) -> Tuple[Tuple[int, int], ...]:
  assert len(shape) == len(strides)
  ret = [(shape[0], strides[0])] if shape else []
  for i in range(1, len(shape)):
    if ret[-1][1] == shape[i]*strides[i] or ret[-1][0] == 1:
      ret[-1] = (ret[-1][0] * shape[i], strides[i])
    elif shape[i] == 1:
      continue
    else:
      ret.append((shape[i], strides[i]))
  return tuple(ret)

@functools.lru_cache(maxsize=None)
def filter_strides(shape:Tuple[int, ...], strides:Tuple[int, ...]) -> Tuple[int, ...]:
  return tuple(stride if shp != 1 else 0 for stride, shp in zip(strides, shape))

@functools.lru_cache(maxsize=None)
def strides_for_shape(shape:Tuple[int, ...]) -> Tuple[int, ...]:
  strides = [1] if shape else []
  for d in shape[::-1][:-1]: strides = [d*strides[0]] + strides
  return filter_strides(shape, tuple(strides))

# symbolic int
sint = Union[Node, int]

class View(NamedTuple):
  shape:Tuple[sint, ...]
  strides:Tuple[sint, ...]
  offset:sint
  mask:Optional[Tuple[Tuple[sint, sint], ...]]
  contiguous:bool

  @staticmethod
  @functools.lru_cache(maxsize=None)
  def create(shape:Tuple[sint, ...], strides:Optional[Tuple[sint, ...]]=None, offset:sint=0, mask:Optional[Tuple[Tuple[sint, sint], ...]]=None):
    strides = filter_strides(shape, strides) if strides else strides_for_shape(shape)
    contiguous = offset == 0 and mask is None and all(s1 == s2 for s1,s2 in zip(strides, strides_for_shape(shape)))
    return View(shape, strides, offset, mask, contiguous)

  def expr_node_mask(self, idx, valid=None) -> Node:
    expr = [valid] if valid is not None else []
    if self.mask is not None:
      acc = 1
      for ns,(x,y) in reversed(list(zip(self.shape, self.mask))):
        if x != 0 or y != ns:
          base = ((idx//acc) % ns)
          expr += [base >= x, base < y]
        acc *= ns
    return Variable.ands(expr)

  # generate an expression if you have a single idx variable
  def expr_node(self, idx=None) -> Node:
    if idx is None: idx = Variable('idx', 0, prod(self.shape)-1)
    ret: List[Node] = [Variable.num(self.offset) if isinstance(self.offset, int) else self.offset] if self.offset else []
    acc = 1
    for d,s in reversed(to_shape_strides(self.shape, self.strides)):
      ret.append(((idx//acc)%d)*s)
      acc *= d
    return Variable.sum(ret)

  # generate an expression if you have a variable or expression for each index
  def expr_idxs(self, idxs) -> Node:
    assert len(idxs) == len(self.shape), f"need an idx for all dimensions {idxs} vs {self.shape}"
    return Variable.sum([Variable.num(self.offset) if isinstance(self.offset, int) else self.offset] + [idx*st for idx,sh,st in zip(idxs, self.shape, self.strides) if sh != 1 and st != 0])

  # MovementOps live here now

  def __unsafe_resize(self, arg: Tuple[Tuple[int, int], ...], mask=None) -> View:
    offset = sum([s * x[0] for s, x in zip(self.strides,arg)])
    if self.mask:
      # move the old mask
      nmask = tuple([(max(mx-ax, 0), min(my-ax, ay-ax)) for (mx,my),(ax,ay) in zip(self.mask, arg)])
      # merge the masks if we have two
      mask = tuple([(max(mx1, mx2), min(my1, my2)) for (mx1, my1), (mx2, my2) in zip(nmask, mask)]) if mask is not None else nmask
    return View.create(tuple([y-x for x,y in arg]), self.strides, self.offset+offset, mask)

  @functools.lru_cache(maxsize=None)  # pylint: disable=method-cache-max-size-none
  def pad(self, arg: Tuple[Tuple[int, int], ...]) -> View:
    assert all((b>=0 and e>=0) for b,e in arg) and len(arg) == len(self.shape)
    if any(b or e for b, e in arg):
      zvarg = tuple([(-b,s+e) for s,(b,e) in zip(self.shape, arg)])
      mask = tuple([(b,s+b) for s,(b,_) in zip(self.shape, arg)])
      return self.__unsafe_resize(zvarg, mask=mask)
    return self

  @functools.lru_cache(maxsize=None)  # pylint: disable=method-cache-max-size-none
  def shrink(self, arg: Tuple[Tuple[int, int], ...]) -> View:
    assert all((b>=0 and e<=s) for s,(b,e) in zip(self.shape,arg)) and len(arg) == len(self.shape)
    return self.__unsafe_resize(arg)

  @functools.lru_cache(maxsize=None)  # pylint: disable=method-cache-max-size-none
  def expand(self, new_shape: Tuple[sint, ...]) -> View:
    assert len(new_shape) == len(self.shape)
    assert all(is_sym_int(x) and (s == x or (s == 1 and st == 0)) for s,x,st in zip(self.shape, new_shape, self.strides)), f"can't expand {self.shape} into {new_shape}"
    # NOTE: can the mask ever be (0,0)?
    mask = tuple([(((0,0) if m != (0,1) else (0,ns)) if s != ns else m) for m,s,ns in zip(self.mask, self.shape, new_shape)]) if self.mask else None
    return View.create(new_shape, self.strides, self.offset, mask)

  @functools.lru_cache(maxsize=None)  # pylint: disable=method-cache-max-size-none
  def permute(self, axis: Tuple[int, ...]) -> View:
    assert all(isinstance(x, int) and x >= 0 and x < len(self.shape) for x in axis), f"invalid permute {axis} for {self.shape}"
    assert len(set(axis)) == len(axis) and len(axis) == len(self.shape), f"can't permute {self.shape} with {axis}"
    return View.create(tuple([self.shape[a] for a in axis]), tuple([self.strides[a] for a in axis]), self.offset, tuple([self.mask[a] for a in axis]) if self.mask is not None else None)

  @functools.lru_cache(maxsize=None)  # pylint: disable=method-cache-max-size-none
  def stride(self, mul: Tuple[int, ...]) -> View:
    # except for the negative case, you can build this from the others. invertible in the negative case
    assert all(isinstance(x, int) and x != 0 for x in mul), f"invalid stride {mul} for {self.shape}"
    strides = tuple([z*m for z,m in zip(self.strides, mul)])
    new_shape = tuple([(s+(abs(m)-1))//abs(m) for s,m in zip(self.shape, mul)])
    offset = sum([(s-1)*z for s,z,m in zip(self.shape, self.strides, mul) if m < 0])
    mask = tuple([(((mx if m > 0 else s-my)+(abs(m)-1))//abs(m), ((my if m > 0 else s-mx)+(abs(m)-1))//abs(m)) for (mx,my),s,m in zip(self.mask, self.shape, mul)]) if self.mask is not None else None
    return View.create(new_shape, strides, self.offset + offset, mask)

  @functools.lru_cache(maxsize=None)  # pylint: disable=method-cache-max-size-none
  def reshape(self, new_shape: Tuple[sint, ...]) -> Optional[View]:
    if self.shape == new_shape: return self

    assert all(is_sym_int(x) and x > 0 for x in new_shape), f"shape must be symbolic ints and can't contain 0 or negative numbers {new_shape}"
    # only check size for int shapes. we don't check symbolic here as long as the reshape itself can be done
    if all(isinstance(s, int) for s in self.shape) and all(isinstance(s, int) for s in new_shape):
      assert prod(self.shape) == prod(new_shape), f"can't reshape {self.shape} -> {new_shape}" # type: ignore  # mypy cannot resolve, all ints here

    # after the asserts, it's okay to check contiguous
    if self.contiguous: return View.create(new_shape)

    # check if this is adding or removing 1s (only)
    # NOTE: this is optional, but removes most calls to (expensive!) merge_views (with mask, not optional)
    if [x for x in self.shape if x != 1] == [x for x in new_shape if x != 1]:
      new_strides: List[sint] = [y for x,y in zip(self.shape, self.strides) if x != 1]
      new_strides_tuple: Tuple[sint, ...] = tuple([0 if x == 1 else new_strides.pop(0) for x in new_shape])
      new_mask_tuple: Optional[Tuple[Tuple[sint, sint], ...]] = None
      if self.mask:
        for x,y in zip(self.shape, self.mask):
          if x == 1 and y != (0, 1):
            new_mask_tuple = ((0,0),) * len(new_shape)
            break
        else:
          new_mask: List[Tuple[sint, sint]] = [y for x,y in zip(self.shape, self.mask) if x != 1]
          new_mask_tuple = tuple([(0,1) if x == 1 else new_mask.pop(0) for x in new_shape])
      return View.create(new_shape, new_strides_tuple, self.offset, new_mask_tuple)

    # TODO: bring the merge_views logic here for more caching

    return None