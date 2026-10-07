"use strict";

// Runs under the application's own Node.js or Bun runtime, without dependencies.
const { spawn } = require("node:child_process");
const { Transform } = require("node:stream");
const { constants } = require("node:os");
const MAX_LINE_BYTES = 8192;
const IDLE_MS = 100;

class TimestampLines extends Transform {
  constructor() {
    super();
    this.pending = Buffer.allocUnsafe(MAX_LINE_BYTES);
    this.size = 0;
    this.timestamp = "";
    this.continued = false;
    this.timer = null;
  }

  emitLine(continued = false) {
    this.push(
      Buffer.concat([
        Buffer.from(`${this.timestamp || new Date().toISOString()} `),
        this.pending.subarray(0, this.size),
        Buffer.from("\n"),
      ]),
    );
    this.size = 0;
    this.timestamp = "";
    this.continued = continued;
  }

  _transform(chunk, _encoding, done) {
    clearTimeout(this.timer);
    let offset = 0;
    while (offset < chunk.length) {
      const newline = chunk.indexOf(10, offset);
      const end = newline === -1 ? chunk.length : newline;
      const count = Math.min(end - offset, MAX_LINE_BYTES - this.size);
      if (count) {
        if (!this.size) this.timestamp = new Date().toISOString();
        chunk.copy(this.pending, this.size, offset, offset + count);
        this.size += count;
        offset += count;
      }
      if (offset === newline) {
        if (this.size || !this.continued) this.emitLine();
        this.continued = false;
        offset++;
      } else if (this.size === MAX_LINE_BYTES) {
        // Keep a UTF-8 character together at a size boundary when possible.
        let start = this.size - 1;
        while (start > this.size - 4 && (this.pending[start] & 0xc0) === 0x80)
          start--;
        const lead = this.pending[start];
        const width =
          lead >= 0xf0 && lead <= 0xf4
            ? 4
            : lead >= 0xe0 && lead <= 0xef
              ? 3
              : lead >= 0xc2 && lead <= 0xdf
                ? 2
                : 1;
        const held =
          width > this.size - start
            ? Buffer.from(this.pending.subarray(start, this.size))
            : null;
        if (held) this.size = start;
        this.emitLine(true);
        if (held) {
          held.copy(this.pending);
          this.size = held.length;
          this.timestamp = new Date().toISOString();
        }
      }
    }
    if (this.size) this.timer = setTimeout(() => this.emitLine(true), IDLE_MS);
    done();
  }

  _flush(done) {
    clearTimeout(this.timer);
    if (this.size) this.emitLine();
    done();
  }
}

const [command, ...args] = process.argv.slice(2);
const child = spawn(command, args, {
  detached: true, // A process group includes npm's shell and the app it starts.
  stdio: ["inherit", "pipe", "pipe"],
});
const forward = (signal) => {
  if (!child.pid) return;
  try {
    process.kill(-child.pid, signal);
  } catch (error) {
    if (error.code !== "ESRCH") throw error;
  }
};
for (const signal of ["SIGTERM", "SIGINT"])
  process.on(signal, () => forward(signal));
let streamsDone = 0;
let status = null;
const finish = () => {
  if (!status || streamsDone !== 2) return;
  if (status.signal) {
    process.removeAllListeners(status.signal);
    process.kill(process.pid, status.signal);
    // Linux PID 1 can ignore a default signal: still terminate conventionally.
    setImmediate(() => process.exit(128 + constants.signals[status.signal]));
  } else process.exitCode = status.code;
};
for (const [input, output] of [
  [child.stdout, process.stdout],
  [child.stderr, process.stderr],
]) {
  const lines = new TimestampLines();
  lines.on("end", () => {
    // Wait for the destination to drain before allowing process exit.
    output.write("", () => {
      streamsDone++;
      finish();
    });
  });
  input.pipe(lines).pipe(output, { end: false });
}
child.on("error", () => {
  process.exitCode = 127;
  process.stderr.write(
    `${new Date().toISOString()} Could not start the app command.\n`,
  );
  status = { code: 127, signal: null };
  finish();
});
child.on("close", (code, signal) => {
  status = status || { code: code === null ? 127 : code, signal };
  finish();
});
