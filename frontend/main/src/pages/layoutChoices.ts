export type LayoutDiscriminator = {
  name: string;
  choices: readonly string[];
  proposal?: { value: string; confirmed?: boolean; requires_confirmation?: boolean } | null;
};

/**
 * Pre-select only a deterministic drawing-clue proposal or an already confirmed value.
 *
 * A reader-only suggestion is an abstention until the reviewer actively chooses it. Missing and
 * out-of-set proposals also stay unanswered.
 */
export function layoutChoiceDefaults(
  discriminators: readonly LayoutDiscriminator[],
): Record<string, string> {
  return Object.fromEntries(
    discriminators
      .filter((discriminator) => {
        const value = discriminator.proposal?.value;
        return (
          value !== undefined &&
          discriminator.choices.includes(value) &&
          (discriminator.name !== 'wall_config' ||
            discriminator.proposal?.confirmed === true ||
            discriminator.proposal?.requires_confirmation === false)
        );
      })
      .map((discriminator) => [discriminator.name, discriminator.proposal?.value ?? '']),
  );
}
