import { describe, expect, it } from "vitest";

import { formatDuration, formatTokens } from "@/utils/format";

// The duration column must stay readable across four orders of magnitude: a
// cached reply is ~30 ms, a large reasoning request runs for minutes. Rendering
// everything as raw milliseconds made "412345 ms" the common case.
describe("formatDuration", () => {
  it("renders sub-second values in milliseconds", () => {
    expect(formatDuration(0)).toBe("0 ms");
    expect(formatDuration(37)).toBe("37 ms");
    expect(formatDuration(999)).toBe("999 ms");
  });

  it("switches to seconds at the one-second boundary", () => {
    expect(formatDuration(1000)).toBe("1.0 s");
    expect(formatDuration(1500)).toBe("1.5 s");
    expect(formatDuration(59_940)).toBe("59.9 s");
  });

  it("renders minute-scale values as minutes and seconds", () => {
    expect(formatDuration(60_000)).toBe("1m 0.0s");
    expect(formatDuration(412_345)).toBe("6m 52.3s");
  });

  it("renders a placeholder for absent or non-finite values", () => {
    expect(formatDuration(null)).toBe("--");
    expect(formatDuration(undefined)).toBe("--");
    expect(formatDuration(Number.NaN)).toBe("--");
    expect(formatDuration(Number.POSITIVE_INFINITY)).toBe("--");
  });

  it("keeps the existing token formatting unchanged", () => {
    expect(formatTokens(999)).toBe("999");
    expect(formatTokens(1500)).toBe("1.5K");
    expect(formatTokens(2_000_000)).toBe("2M");
    expect(formatTokens(null)).toBe("--");
  });
});
