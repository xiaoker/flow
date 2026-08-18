/** Normalize user-entered search text without changing the displayed source name. */
export function normalizeSearchText(value: string): string {
  return value
    .normalize("NFKC")
    .replace(/\s+/g, "")
    .toLocaleLowerCase("zh-CN");
}

export function searchTextMatches(query: string, ...values: Array<string | null | undefined>): boolean {
  const needle = normalizeSearchText(query);
  if (!needle) return true;
  return values.some((value) => normalizeSearchText(value ?? "").includes(needle));
}
