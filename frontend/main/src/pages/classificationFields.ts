/** Inputs a reviewer answers by choosing rather than by measuring (#684).
 *
 * A cabinet category has no unit, so it cannot travel as a measurement: the server puts a
 * measurement through the imperial parser and `single_door` is correctly refused. These helpers
 * keep the decision — dimension or choice — in one place, and out of the page.
 *
 * The choices themselves are never listed here. They come from the server, which derives them from
 * the rule's semantic type and validates the submission against the same list, so the form cannot
 * offer something the submission would reject.
 */

/** One quantity as `required-inputs` sends it, narrowed to what these helpers read. */
export type ClassifiableQuantity = {
  key: string;
  many: boolean;
  consumers: { rule_id?: string; input_name?: string }[];
  categories?: string[];
};

export type ClassificationEntry = {
  rule_id: string;
  name: string;
  categories: string[];
};

/** Whether this input is answered by choosing. Non-empty choices is the only signal. */
export function isCategorical(quantity: ClassifiableQuantity): boolean {
  return (quantity.categories ?? []).length > 0;
}

/** A stored category as a person reads it.
 *
 * Derived from the value rather than kept in a lookup: the server sends the list, and a table here
 * would silently show a raw `drawer_base` the day the rulebook offers one.
 */
export function categoryLabel(category: string): string {
  const words = category.replace(/_/g, ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * What to send, from what the reviewer chose.
 *
 * **A run is sent only when every item in it has an answer.** A partial run would store fewer
 * classifications than there are cabinets, and the check then abstains on the length — a truthful
 * outcome reached the slow way, when the form already knows the reviewer is not finished. An
 * untouched run is left out entirely rather than sent as blanks.
 *
 * One chosen run fans out to every rule input it feeds, the same way a measurement does: the
 * mapping is the server's, taken from `consumers`.
 */
export function classificationEntries(
  quantities: ClassifiableQuantity[],
  runs: Record<string, string[]>,
): ClassificationEntry[] {
  return quantities.filter(isCategorical).flatMap((quantity) => {
    const chosen = runs[quantity.key] ?? [];
    if (!chosen.length || chosen.some((value) => !value.trim())) return [];
    return quantity.consumers
      .filter((consumer) => consumer.rule_id && consumer.input_name)
      .map((consumer) => ({
        rule_id: consumer.rule_id as string,
        name: consumer.input_name as string,
        categories: chosen.map((value) => value.trim()),
      }));
  });
}
