# Usual vendor mistakes, and how to spot each

Each entry: how it looks, how to spot it, where it lands. "Red" means code can prove it from
printed values (the vendor's value and what it should be); "grey" means it is listed for a person.
Code decides which; your job is to find it and name the spots.

## Numbers

**A string that does not add up.** The pieces of a vendor chain do not add up to its overall.
Spot: name every part's label and the overall's label; code adds them. A part drawn shorter than
the overall by the same amount (an overhang, an inset) is not one string. Red when every value is
printed.

**A segment changed inside a chain.** The overall matches but one segment moved (wall to outlet 1'-5"
instead of 1'-1"). Spot: compare each segment of the architect's chain on its own, never only the
total. Red when both labels are printed and both picks agree.

**A locating dimension changed.** An outlet, sink or faucet centre line moved; the height AFF to an
outlet differs; the vendor measured from the cabinet end instead of the wall. Spot: check where each
locating dimension starts.

**A thing left out.** The architect locates an outlet, J-box, grommet or cut-out; the vendor shows
none. Spot: every locating dimension on the architect's side needs its thing on the vendor's. The
missing thing is its own item, with the architect's values.

**A drift of 1/8" or 1/4".** A value off by a small fraction. Spot: compare exactly; GV allows no
tolerance unless rules.md names one. Check the stacked fraction was read right (zoom).

**Plan and elevation disagree.** The vendor prints one size in plan and another in elevation for the
same piece. Spot: the same item in every view; code compares readings across views.

**Millimetres and inches disagree.** `660 [27]`. Spot: mm / 25.4 against the inches. A sign of a
typing slip; inches still count.

**A height that includes something else.** A splash, a top thickness or a box above the unit inside
one side's height (what-dimensions-measure.md). Spot: say where each end lands before pairing.

**A sink cut-out or rim that does not fit.** A rim collar larger than the hole it sits in is right; a
collar smaller is not. Cut-out = the sink's inside size less GV's clearance each side (rules.md).

**A round-2 change nobody asked for.** Round 2 fixes the item GV flagged but moves another. Spot:
compare round 2 with the architect again, not only with round 1.

**A scaled size.** Where the architect prints no size, a measured size is "about X" and only ever
Not compared, never Wrong.

## Drawn and written

**A note not followed.** The architect's note says "pull-out drawers" or "(2) adjustable shelves";
the vendor draws hinged doors or fixed shelves. Spot: read every architect note and compare it with
what is drawn, even when the count of fronts matches. Grey.

**A count or arrangement that differs.** Three drawers instead of four; two doors instead of one
pair; equal bays where the architect draws unequal ones. Spot: count fronts, bays, shelves and
their order left to right, top to bottom. Grey.

**A side splash at a free-standing end.** A side splash belongs only where the top meets a wall.
Spot: look at the plan for walls at each end. Grey.

**A tag different or missing.** The architect tags a top `ST-2` and the vendor `ST-3`, or prints no
tag. Spot: compare each piece's tag; a different name may be the same finish only when a printed
legend says so. Red only when both tags are printed text.

**Hardware or accessory different.** Pull type or size, a cam lock, a grommet, legs, a power unit.
Grey.

**A construction note contradicted.** "Toe kick integrated with door", "door to the floor", "metal
lining". Grey.

**Position.** An item that must be centred or aligned (a microwave over a range, a sink on its
centre line) is not; a piece collides with a neighbour or misses its opening. Grey.

**Extent.** A section the architect draws is missing; the vendor adds pieces; an island faces the
wrong way. Grey.

**The ID set against the brand prototype.** The ID set governs wherever it gives information
(never a question of which governs): where it shows the item, a difference from the prototype alone
is not compared. **When the architect gives no elevation of the item, the brand prototype elevation
is the reference** (as GV's reviewer does): compare the vendor with the prototype and report it
with `source: brand_prototype`, like an architect comparison; code checks the page map and the
proof says "reference: brand prototype". The prototype also governs where the ID set points to the
brand standard ("per brand standard").

**GV's own note taken as the vendor's.** A coloured value, or a callout ending "by GV", is GV's
markup. Never report it as the vendor's value or tag.
