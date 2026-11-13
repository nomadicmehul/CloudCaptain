/** "45 min" for lessons, "10 h" for capstones (whole hours from 4 h up). */
export function formatDuration(minutes: number): string {
  return minutes >= 240 && minutes % 60 === 0 ? `${minutes / 60} h` : `${minutes} min`;
}
