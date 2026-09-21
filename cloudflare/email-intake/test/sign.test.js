import { strict as assert } from "node:assert";
import { test } from "node:test";

import { sign } from "../src/sign.js";

// The same vector tests/test_intake.py hard-codes (_VECTOR_SECRET,
// _VECTOR_TIMESTAMP, _VECTOR_BODY, _VECTOR_HEADER there): secret "s",
// timestamp "1700000000", body "hello". Two independent implementations of
// one construction are only the same construction while both agree on a
// fixed answer, so the hex is written out here rather than recomputed.
const SECRET = "s";
const TIMESTAMP = "1700000000";
const BODY = "hello";
const EXPECTED =
  "7caaa0c43407622ac33872a98db85fa647961b1a4572722c3bb938e5114b1022";

const bytes = (text) => new TextEncoder().encode(text);

test("the signature vector matches the app's", async () => {
  assert.equal(await sign(SECRET, TIMESTAMP, bytes(BODY)), EXPECTED);
});

test("the hex carries no prefix — the worker adds `sha256=`", async () => {
  const hex = await sign(SECRET, TIMESTAMP, bytes(BODY));
  assert.match(hex, /^[0-9a-f]{64}$/);
});

test("the separator is signed: the timestamp cannot be recut into the body", async () => {
  // "1700000000" + "\n" + "hello" and "170000000" + "0\nhello" are the same
  // bytes under a construction that merely concatenates. They must not be the
  // same MAC.
  const shifted = await sign(SECRET, "170000000", bytes("0\nhello"));
  assert.notEqual(shifted, EXPECTED);
});

test("a different secret gives a different MAC", async () => {
  assert.notEqual(await sign("s2", TIMESTAMP, bytes(BODY)), EXPECTED);
});

test("a string body is refused, not signed as zeros", async () => {
  // `Uint8Array.prototype.set` ignores a string, so without the guard this
  // would quietly MAC a run of zero bytes — the same MAC for every message.
  await assert.rejects(() => sign(SECRET, TIMESTAMP, BODY), TypeError);
  await assert.rejects(() => sign(SECRET, TIMESTAMP, undefined), TypeError);

  // And the zeros it would otherwise have produced are not the right answer,
  // which is what makes the guard worth having rather than pedantry.
  const zeros = await sign(SECRET, TIMESTAMP, new Uint8Array(BODY.length));
  assert.notEqual(zeros, EXPECTED);
});

test("an empty body signs", async () => {
  const hex = await sign(SECRET, TIMESTAMP, new Uint8Array(0));
  assert.match(hex, /^[0-9a-f]{64}$/);
});

test("the body is signed as bytes, not round-tripped through text", async () => {
  // A raw message is bytes, and a night audit's attachments are binary. If
  // this ever decoded the body to a string first, the invalid-UTF-8 bytes
  // below would come back as U+FFFD and sign as something else, so the two
  // MACs here would collide. They must not.
  const raw = new Uint8Array([0xff, 0xfe, 0x00, 0x41]);
  const decoded = new TextEncoder().encode(new TextDecoder().decode(raw));
  assert.notDeepEqual(Array.from(raw), Array.from(decoded));
  assert.notEqual(
    await sign(SECRET, TIMESTAMP, raw),
    await sign(SECRET, TIMESTAMP, decoded),
  );
});
