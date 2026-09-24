export type LayoutDiscriminator = {
  name: string;
  choices: readonly string[];
  proposal?: { value: string } | null;
};

/**
 * Pre-select only a filed proposal that is one of the rulebook's closed choices.
 *
 * A missing proposal is an abstention, so it stays unanswered. A value outside the closed set is
 * also left unanswered: the resolver would reject it anyway, and pre-filling it would turn a model
 * mistake into reviewer anchoring.
 */
export function layoutChoiceDefaults(
  discriminators: readonly LayoutDiscriminator[],
): Record<string, string> {
  return Object.fromEntries(
    discriminators
      .filter((discriminator) => {
        const value = discriminator.proposal?.value;
        return value !== undefined && discriminator.choices.includes(value);
      })
      .map((discriminator) => [discriminator.name, discriminator.proposal?.value ?? '']),
  );
}
