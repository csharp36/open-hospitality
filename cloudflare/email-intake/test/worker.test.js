/**
 * The email handler, exercised with a fake `message` and a stubbed `fetch`.
 *
 * No wrangler, no miniflare, no network: `node --test` on Node 22 has
 * `ReadableStream`, `Headers`, `Response` and `crypto.subtle` as globals,
 * which is the whole of the Workers surface this worker touches. That is
 * deliberate — CI runs this on every pull request, and a test that needed the
 * Cloudflare runtime would need a credential too.
 */

import { strict as assert } from "node:assert";
import { afterEach, beforeEach, test } from "node:test";

import worker from "../src/worker.js";
import { sign } from "../src/sign.js";

const FALLBACK = "ops-fallback@example.test";

const ENV = {
  INTAKE_URL: "https://app.example.test/api/intake/email",
  INTAKE_SECRET: "s",
  FALLBACK_ADDRESS: FALLBACK,
  MAX_BYTES: 26214400,
};

const OUTCOME = { outcome: "ingested", attachments: [] };

const encoder = new TextEncoder();

/** A stream that hands the bytes over in several chunks, as a real one does. */
function streamOf(bytes, chunkSize = 7) {
  return new ReadableStream({
    start(controller) {
      for (let at = 0; at < bytes.length; at += chunkSize) {
        controller.enqueue(bytes.slice(at, at + chunkSize));
      }
      controller.close();
    },
  });
}

function fakeMessage({
  raw = encoder.encode("From: pms@hotel.test\r\n\r\nnight audit"),
  rawSize,
  from = "pms@hotel.test",
  to = "na-abc@intake.example.test",
  auth = "mx.cloudflare.net; dkim=pass header.d=hotel.test",
} = {}) {
  const forwarded = [];
  const rejected = [];
  return {
    from,
    to,
    rawSize: rawSize === undefined ? raw.length : rawSize,
    raw: streamOf(raw),
    headers: new Headers(auth === null ? {} : { "authentication-results": auth }),
    async forward(address) {
      forwarded.push(address);
    },
    setReject(reason) {
      rejected.push(reason);
    },
    // Not part of the Cloudflare surface — the spies this test reads.
    forwarded,
    rejected,
    rawBytes: raw,
  };
}

let calls;
let logged;
let realFetch;
let realLog;
let realError;

beforeEach(() => {
  calls = [];
  logged = [];
  realFetch = globalThis.fetch;
  realLog = console.log;
  realError = console.error;
  console.log = (...args) => logged.push(args.join(" "));
  console.error = () => {};
});

afterEach(() => {
  globalThis.fetch = realFetch;
  console.log = realLog;
  console.error = realError;
});

/** Record every request and answer with `response`. */
function stubFetch(response) {
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    if (typeof response === "function") {
      return response();
    }
    return response;
  };
}

const ok = () =>
  new Response(JSON.stringify(OUTCOME), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });

test("a 2xx is the end of it: no forward, and the outcome is logged", async () => {
  stubFetch(ok);
  const message = fakeMessage();

  await worker.email(message, ENV, {});

  assert.equal(calls.length, 1);
  assert.deepEqual(message.forwarded, []);
  assert.deepEqual(logged, [JSON.stringify(OUTCOME)]);
});

test("the five headers carry the envelope values and a valid signature", async () => {
  stubFetch(ok);
  const message = fakeMessage();

  await worker.email(message, ENV, {});

  const { url, init } = calls[0];
  assert.equal(url, ENV.INTAKE_URL);
  assert.equal(init.method, "POST");
  assert.equal(init.headers["Content-Type"], "message/rfc822");

  assert.equal(init.headers["X-Intake-To"], "na-abc@intake.example.test");
  assert.equal(init.headers["X-Intake-From"], "pms@hotel.test");
  assert.equal(
    init.headers["X-Intake-Auth"],
    "mx.cloudflare.net; dkim=pass header.d=hotel.test",
  );

  const timestamp = init.headers["X-Intake-Timestamp"];
  assert.match(timestamp, /^\d{1,12}$/);
  // Unix seconds for right now, not milliseconds and not a stale constant:
  // the app refuses a timestamp more than 300 s from its own clock.
  assert.ok(Math.abs(Number(timestamp) - Date.now() / 1000) < 60);

  const expected = await sign(ENV.INTAKE_SECRET, timestamp, message.rawBytes);
  assert.equal(init.headers["X-Intake-Signature"], `sha256=${expected}`);
});

test("the body is the raw bytes, unchanged and in order", async () => {
  stubFetch(ok);
  // Long enough to span several chunks of the fake stream, and carrying bytes
  // no text encoding round-trips.
  const raw = new Uint8Array(200);
  for (let i = 0; i < raw.length; i += 1) {
    raw[i] = (i * 7) % 256;
  }
  const message = fakeMessage({ raw });

  await worker.email(message, ENV, {});

  assert.deepEqual(Array.from(calls[0].init.body), Array.from(raw));
});

test("a missing Authentication-Results is sent as an empty header", async () => {
  stubFetch(ok);

  await worker.email(fakeMessage({ auth: null }), ENV, {});

  assert.equal(calls[0].init.headers["X-Intake-Auth"], "");
});

test("a non-2xx forwards to the fallback", async () => {
  stubFetch(() => new Response("nope", { status: 500 }));
  const message = fakeMessage();

  await worker.email(message, ENV, {});

  assert.deepEqual(message.forwarded, [FALLBACK]);
  assert.deepEqual(logged, []);
});

test("a 401 forwards too — a refused signature is not a disposition", async () => {
  stubFetch(() => new Response("", { status: 401 }));
  const message = fakeMessage();

  await worker.email(message, ENV, {});

  assert.deepEqual(message.forwarded, [FALLBACK]);
});

test("a fetch that throws forwards to the fallback", async () => {
  globalThis.fetch = async () => {
    calls.push({});
    throw new TypeError("network");
  };
  const message = fakeMessage();

  await worker.email(message, ENV, {});

  assert.equal(calls.length, 1);
  assert.deepEqual(message.forwarded, [FALLBACK]);
  assert.deepEqual(logged, []);
});

test("over MAX_BYTES forwards without POSTing anything", async () => {
  stubFetch(ok);
  const env = { ...ENV, MAX_BYTES: 64 };
  const message = fakeMessage({ raw: new Uint8Array(500) });

  await worker.email(message, env, {});

  assert.deepEqual(calls, []);
  assert.deepEqual(message.forwarded, [FALLBACK]);
});

test("over MAX_BYTES is caught by the stream too, with rawSize absent", async () => {
  // A Cloudflare message always carries rawSize; this is the path that holds
  // if it ever does not, and it is what bounds the read to the cap plus one
  // chunk rather than the whole message.
  stubFetch(ok);
  const env = { ...ENV, MAX_BYTES: 64 };
  const message = fakeMessage({ raw: new Uint8Array(500), rawSize: null });

  await worker.email(message, env, {});

  assert.deepEqual(calls, []);
  assert.deepEqual(message.forwarded, [FALLBACK]);
});

test("a message exactly at MAX_BYTES is posted, not forwarded", async () => {
  stubFetch(ok);
  const env = { ...ENV, MAX_BYTES: 64 };
  const message = fakeMessage({ raw: new Uint8Array(64) });

  await worker.email(message, env, {});

  assert.equal(calls.length, 1);
  assert.deepEqual(message.forwarded, []);
});

test("setReject is never called, on any path", async () => {
  const paths = [
    () => stubFetch(ok),
    () => stubFetch(() => new Response("", { status: 500 })),
    () => {
      globalThis.fetch = async () => {
        throw new TypeError("network");
      };
    },
  ];
  for (const arrange of paths) {
    arrange();
    const message = fakeMessage();
    await worker.email(message, ENV, {});
    assert.deepEqual(message.rejected, []);
  }

  const oversized = fakeMessage({ raw: new Uint8Array(500) });
  await worker.email(oversized, { ...ENV, MAX_BYTES: 64 }, {});
  assert.deepEqual(oversized.rejected, []);
});

test("the message is forwarded at most once", async () => {
  stubFetch(() => new Response("", { status: 503 }));
  const message = fakeMessage();

  await worker.email(message, ENV, {});

  assert.equal(message.forwarded.length, 1);
});

test("console.log carries the app's outcome and nothing from the message", async () => {
  stubFetch(ok);
  const secret = "4111111111111111 is in the body";
  const message = fakeMessage({ raw: encoder.encode(secret) });

  await worker.email(message, ENV, {});

  assert.deepEqual(logged, [JSON.stringify(OUTCOME)]);
  assert.ok(!logged.join("\n").includes("4111111111111111"));
});

test("a 2xx whose body is not JSON is still accepted, and logs nothing", async () => {
  stubFetch(() => new Response("<html>proxy</html>", { status: 200 }));
  const message = fakeMessage();

  await worker.email(message, ENV, {});

  assert.deepEqual(message.forwarded, []);
  assert.deepEqual(logged, []);
});
