/**
 * Where a setting came from (#827): which source the form sends, and which settings still need one.
 *
 * The server lists each setting's allowed sources, in the order Raj's checklist gives them, and
 * refuses any other. One allowed source needs no question, so it is sent without asking; several
 * need the reviewer's answer, because which is true is a fact about the job the system cannot know.
 */

export interface SettingSource {
  value: string;
  guidance: string;
}

export interface SourcedSetting {
  name: string;
  sources?: SettingSource[];
}

/** The source to send for a setting, or `null` when the reviewer has not chosen one of several. */
export function sourceToSend(setting: SourcedSetting, chosen: string | undefined): string | null {
  const allowed = setting.sources ?? [];
  if (allowed.length === 1) return allowed[0].value;
  if (chosen && allowed.some((source) => source.value === chosen)) return chosen;
  return null;
}

/**
 * The first filled-in setting whose source is still unanswered, so the form can say so before
 * saving rather than send a value the server will refuse. `null` when every filled one is answered.
 */
export function settingMissingASource(
  settings: SourcedSetting[],
  typed: Record<string, string>,
  chosen: Record<string, string>,
): string | null {
  for (const setting of settings) {
    if (!(typed[setting.name] ?? '').trim()) continue;
    if ((setting.sources ?? []).length > 1 && sourceToSend(setting, chosen[setting.name]) === null) {
      return setting.name;
    }
  }
  return null;
}
