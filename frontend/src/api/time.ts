/** Python 将 UTC 时间存为无时区 DATETIME；HTTP ISO 字符串须按 UTC 解释。 */
export function utcIsoToEpoch(value: string | null | undefined): number | null {
  if (!value) return null;
  const explicitZone = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value);
  const timestamp = Date.parse(explicitZone ? value : `${value}Z`);
  return Number.isNaN(timestamp) ? null : timestamp;
}
