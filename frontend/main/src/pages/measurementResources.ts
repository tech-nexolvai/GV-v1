export type Available<T> = { value: T; error: null } | { value: null; error: string };

function available<T>(result: PromiseSettledResult<T>): Available<T> {
  return result.status === 'fulfilled'
    ? { value: result.value, error: null }
    : { value: null, error: result.reason instanceof Error ? result.reason.message : String(result.reason) };
}

/** The rulebook's fields are required. A refused reading list is unavailable, never an empty list. */
export async function loadMeasurementResources<Fields, Readings, Vocabulary>(
  fields: Promise<Fields>, readings: Promise<Readings>, vocabulary: Promise<Vocabulary>,
): Promise<{ fields: Fields; readings: Available<Readings>; vocabulary: Available<Vocabulary> }> {
  const [required, read, types] = await Promise.allSettled([fields, readings, vocabulary]);
  if (required.status === 'rejected') throw required.reason;
  return { fields: required.value, readings: available(read), vocabulary: available(types) };
}
