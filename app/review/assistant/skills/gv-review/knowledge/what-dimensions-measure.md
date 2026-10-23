# What a dimension measures

Two numbers can only be compared when both measure the same thing: the same two end points. Most
wrong calls on millwork drawings come from pairing two dimensions whose ends land in different
places. Before you pair two heights or widths, say where each end lands.

## The usual end points, and how to see each
- **floor**: the finished floor line at the bottom of the view.
- **top_of_counter**: the top face of the countertop (the long top line of the top's band).
- **underside_of_counter**: the bottom face of the top, where it sits on the cabinet box. A top's
  thickness (often 3/4" to 1 1/4") separates it from top_of_counter.
- **top_of_splash**: the top of a backsplash standing on the counter. Its line is usually set back
  and shorter than the counter's line.
- **wall_face**: the finished wall at a run's end.
- **cabinet_side / cabinet_end**: the outside face of a cabinet box, or the end of a run.
- **centre_line**: the centre of an outlet, sink, faucet or appliance (CL, ℄).
- **box_above_unit**: a soffit, bulkhead, blocking or box above or behind the unit: the dimension
  runs past the unit itself.
- **blocking**: wood blocking in the wall the unit fixes to.
- **other**: anything else; say what in a few words.

## Worked examples (illustrative, invented numbers)

**1. A splash inside a height.** The architect prints 2'-11 3/8" from the floor to the top line of
the counter run. The vendor prints 34 1/8" to the stone's underside, then 1 1/4" stone, then a
3 1/2" splash, with 38 7/8" overall. Which vendor number goes with 2'-11 3/8"? The architect's top
line is the top of the stone, not of the splash (the splash line is set back and shorter). So pair
2'-11 3/8" (35 3/8") with 34 1/8 + 1 1/4 = 35 3/8": they agree. Pairing it with the 38 7/8" overall
would make a false Wrong. Spans: architect `floor -> top_of_counter`, vendor overall
`floor -> top_of_splash`.

**2. Underside plus the top.** The architect prints 2'-10 3/4" from the floor to a counter. The
vendor prints 33 5/8" to the underside of a 1 1/8" top. 33 5/8 + 1 1/8 = 34 3/4 = 2'-10 3/4". If the
architect's line ends on the top face, the two agree; if it ends on the underside, they differ by
1 1/8". Look at the architect's extension line before you call it Wrong.

**3. A run to a box above the unit.** The architect prints 7'-3" from the floor to a line above a
tall cabinet. The vendor's cabinet is 6'-11" tall. The architect's extension line stops at a soffit
box over the cabinet, not on the cabinet's top. Spans: architect `floor -> box_above_unit`, vendor
`floor -> cabinet_side`. Different extent: not Wrong; it goes on the list for a person.

**4. A locating dimension read as a width.** The architect prints 1'-3" from the wall to the centre
line of an outlet, and 1'-7" AFF. The vendor shows no outlet. The 1'-3" is the outlet's position,
not a width: the item is "outlet not shown on the vendor", with both architect values.

## Rules of thumb
- Equal values with different spans are not a match; different values with different spans are not
  a difference. Only the same two end points compare.
- When the ends are not obvious, follow each extension line to the edge it starts from (zoom with
  guides on the label).
- When you cannot tell where an end lands, still report the item, as not compared (status check), and say which
  end is unclear. A doubt is listed for a person; an item left out is never found again.

## The spans words
`top_of_counter | underside_of_counter | top_of_splash | floor | wall_face | cabinet_side |
cabinet_end | centre_line | box_above_unit | blocking | other`
