import fc from "fast-check";
import { describe, expect, it } from "vitest";
import { HandleError, handle, handles, isNodeId, newId, resolveHandle } from "./ids.ts";

function expectHandleError(fn: () => unknown, code: HandleError["code"]): HandleError {
  try {
    fn();
  } catch (err) {
    expect(err).toBeInstanceOf(HandleError);
    expect((err as HandleError).code).toBe(code);
    return err as HandleError;
  }
  throw new Error("expected a HandleError");
}

describe("newId", () => {
  it("generates UUIDv7s that sort in creation order, even within one millisecond", () => {
    const ids = Array.from({ length: 2000 }, newId);
    expect(ids.every(isNodeId)).toBe(true);
    expect([...ids].sort()).toEqual(ids);
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe("isNodeId", () => {
  it.each([
    ["0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a44", true],
    ["0192F3A1-7C4E-7B91-A2D5-3F8E1C0B9A44", false], // stored IDs are lowercase
    ["0192f3a1-7c4e-4b91-a2d5-3f8e1c0b9a44", false], // v4
    ["0192f3a1-7c4e-7b91-c2d5-3f8e1c0b9a44", false], // wrong variant
    ["0192f3a17c4e7b91a2d53f8e1c0b9a44", false],
    ["../../etc/passwd", false],
  ])("%s → %s", (id, ok) => {
    expect(isNodeId(id)).toBe(ok);
  });
});

describe("handle", () => {
  it("is the last 8 hex chars", () => {
    expect(handle("0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a44")).toBe("1c0b9a44");
  });
});

describe("handles", () => {
  it("lengthens only the handles that collide", () => {
    const a = "0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a44";
    const b = "0192f3a2-0000-7000-8000-00001c0b9a44"; // same last 8 as a
    const c = "0192f3a3-0000-7000-8000-000000000001";
    expect(handles([a, b, c])).toEqual(
      new Map([
        [a, "e1c0b9a44"],
        [b, "01c0b9a44"],
        [c, "00000001"],
      ]),
    );
  });

  it("gives every ID a unique handle that resolves back to it", () => {
    fc.assert(
      fc.property(fc.uniqueArray(fc.uuid({ version: 7 }), { maxLength: 30 }), (ids) => {
        const shown = handles(ids);
        expect(new Set(shown.values()).size).toBe(ids.length);
        for (const id of ids) expect(resolveHandle(shown.get(id) as string, ids)).toBe(id);
      }),
    );
  });
});

describe("resolveHandle", () => {
  const ids = [
    "0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a44",
    "0192f3a1-7c4e-7b91-a2d5-0000aaaa1234",
    "0192f3a1-7c4e-7b91-a2d5-0000bbbb1234",
  ];

  it("accepts the handle, a shorter unique suffix, or the full ID", () => {
    expect(resolveHandle("1c0b9a44", ids)).toBe(ids[0]);
    expect(resolveHandle("9a44", ids)).toBe(ids[0]);
    expect(resolveHandle("aaaa1234", ids)).toBe(ids[1]);
    expect(resolveHandle(ids[0] as string, ids)).toBe(ids[0]);
  });

  it("ignores case, hyphens and surrounding whitespace", () => {
    expect(resolveHandle(" 1C0B9A44 ", ids)).toBe(ids[0]);
    expect(resolveHandle("a2d5-3f8e1c0b9a44", ids)).toBe(ids[0]);
    expect(resolveHandle("0192F3A17C4E7B91A2D53F8E1C0B9A44", ids)).toBe(ids[0]);
  });

  it("reports ambiguity with every candidate", () => {
    const err = expectHandleError(() => resolveHandle("1234", ids), "ambiguous");
    expect(err.candidates).toEqual([ids[1], ids[2]]);
    expect(err.message).toContain("aaaa1234");
    expect(err.message).toContain("bbbb1234");
  });

  it("does not match on the leading (timestamp) chars", () => {
    // These share their first 8 chars, as nodes created close together do.
    expectHandleError(() => resolveHandle("0192f3a1", ids), "unknown");
  });

  it.each(["", "abc", "xyz12345", "1c0b 9a44", "0".repeat(33)])(
    "rejects %j as invalid",
    (input) => {
      expectHandleError(() => resolveHandle(input, ids), "invalid");
    },
  );

  it("reports unknown handles", () => {
    expectHandleError(() => resolveHandle("deadbeef", ids), "unknown");
    expectHandleError(() => resolveHandle("deadbeef", []), "unknown");
  });
});
