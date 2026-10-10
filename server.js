"use strict";

// Operator rehearsal probe for storage migrations. It seeds deterministic rows
// once, then reports counts, checksums and whether writes currently succeed.
const crypto = require("node:crypto");
const http = require("node:http");
const net = require("node:net");
const { Client } = require("pg");
const { MongoClient } = require("mongodb");

const port = Number(process.env.PORT || 8080);
const token = process.env.PROBE_TOKEN || "";
const ROWS = 1000;
let ready = false;

const checksum = (values) => crypto.createHash("sha256").update(values.join("\n")).digest("hex");

async function withPostgres(work) {
  const client = new Client({ connectionString: process.env.DATABASE_URL });
  await client.connect();
  try {
    return await work(client);
  } finally {
    await client.end();
  }
}

const mongo = new MongoClient(process.env.MONGODB_URI || "mongodb://invalid", {
  serverSelectionTimeoutMS: 5000,
});
const docs = () => mongo.db().collection("probe_docs");

async function seed() {
  await seedPostgres().catch((error) => {
    throw new Error(`postgres: ${error.message}`);
  });
  await seedMongo().catch((error) => {
    throw new Error(`mongo: ${error.message}`);
  });
}

async function seedPostgres() {
  await withPostgres(async (client) => {
    await client.query("CREATE TABLE IF NOT EXISTS probe_rows (id integer PRIMARY KEY, value text NOT NULL)");
    const { rows } = await client.query("SELECT count(*)::int AS n FROM probe_rows");
    if (rows[0].n === 0) {
      for (let id = 0; id < ROWS; id += 1) {
        await client.query("INSERT INTO probe_rows (id, value) VALUES ($1, $2)", [id, `row-${id}`]);
      }
    }
  });
}

async function seedMongo() {
  await mongo.connect();
  if ((await docs().countDocuments()) === 0) {
    await docs().insertMany(Array.from({ length: ROWS }, (_, id) => ({ _id: id, value: `doc-${id}` })));
  }
}

async function check() {
  const result = {};
  result.postgres = await withPostgres(async (client) => {
    const { rows } = await client.query("SELECT id, value FROM probe_rows ORDER BY id");
    let write = "ok";
    try {
      await client.query("CREATE TEMP TABLE probe_write (x int)");
    } catch (error) {
      write = error.code || "failed";
    }
    return { rows: rows.length, checksum: checksum(rows.map((r) => `${r.id}:${r.value}`)), write };
  });
  const all = await docs().find({}, { sort: { _id: 1 } }).toArray();
  let write = "ok";
  try {
    await docs().insertOne({ _id: `write-${Date.now()}`, value: "x" });
    await docs().deleteMany({ value: "x" });
  } catch (error) {
    write = error.codeName || "failed";
  }
  const seeded = all.filter((doc) => typeof doc._id === "number");
  result.mongo = { docs: seeded.length, checksum: checksum(seeded.map((d) => `${d._id}:${d.value}`)), write };
  return result;
}

const authorized = (url) => token.length >= 16 && url.searchParams.get("token") === token;

function connectProbe(host, target) {
  return new Promise((resolve) => {
    const socket = net.connect({ host, port: target, timeout: 3000 });
    socket.once("connect", () => {
      socket.destroy();
      resolve("connected");
    });
    socket.once("timeout", () => {
      socket.destroy();
      resolve("timeout");
    });
    socket.once("error", (error) => resolve(error.code || "error"));
  });
}

async function grow(megabytes) {
  const chunk = "x".repeat(1024 * 1024);
  for (let index = 0; index < megabytes; index += 1) {
    await docs().insertOne({ _id: `grow-${Date.now()}-${index}`, blob: chunk });
  }
}

http
  .createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    const reply = (status, body) => {
      response.writeHead(status, { "content-type": "application/json" });
      response.end(JSON.stringify(body) + "\n");
    };
    try {
      if (url.pathname === "/ready") return reply(ready ? 200 : 503, { ready });
      if (url.pathname === "/check") return reply(200, await check());
      if (!authorized(url)) return reply(404, { error: "not found" });
      if (url.pathname === "/connect") {
        const target = Number(url.searchParams.get("port"));
        return reply(200, { result: await connectProbe(url.searchParams.get("host"), target) });
      }
      if (url.pathname === "/grow") {
        await grow(Math.min(Number(url.searchParams.get("mb")) || 1, 4096));
        return reply(200, { grown: true });
      }
      if (url.pathname === "/shrink") {
        await docs().deleteMany({ blob: { $exists: true } });
        return reply(200, { shrunk: true });
      }
      return reply(404, { error: "not found" });
    } catch (error) {
      return reply(500, { error: String(error.codeName || error.code || error.message).slice(0, 200) });
    }
  })
  .listen(port, "0.0.0.0", () => console.log(`listening on ${port}`));

for (const [name, value] of [["DATABASE_URL", process.env.DATABASE_URL], ["MONGODB_URI", process.env.MONGODB_URI]]) {
  try {
    const parsed = new URL(value);
    console.log(`${name} host=${parsed.hostname} port=${parsed.port} params=${[...parsed.searchParams.keys()].join(",")}`);
  } catch {
    console.log(`${name} unparseable or missing`);
  }
}
seed()
  .then(() => {
    ready = true;
    console.log("seeded");
  })
  .catch((error) => console.error("seed failed", error.message));

process.on("SIGTERM", () => {
  mongo.close().finally(() => process.exit(0));
});
