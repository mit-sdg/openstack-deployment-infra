import { expect, test, type Page } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import path from "node:path";
import { spawn } from "node:child_process";

const screenshots = path.resolve("../../.tmp/dashboard-screenshots/after");
async function monitor(page: Page, origin = "http://127.0.0.1:8480") {
  const violations: string[] = [],
    unexpected: string[] = [],
    errors: string[] = [];
  await page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (event) =>
      console.error("DASHBOARD_CSP:" + event.violatedDirective),
    );
  });
  page.on("console", (message) => {
    if (message.text().startsWith("DASHBOARD_CSP:"))
      violations.push(message.text());
  });
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (
      url.origin !== origin ||
      !/^\/$|^\/index.html$|^\/theme.js$|^\/favicon.svg$|^\/assets\/[^/]+\.(?:js|css|svg)$|^\/api\/(?:snapshot|refresh)$/.test(
        url.pathname,
      )
    )
      unexpected.push(request.url());
  });
  return () => {
    expect(violations).toEqual([]);
    expect(unexpected).toEqual([]);
    expect(errors).toEqual([]);
  };
}
for (const width of [320, 390, 768, 1440])
  for (const colorScheme of ["light", "dark"] as const) {
    test(`${width}px ${colorScheme}: operator sections, interactions and CSP`, async ({
      browser,
    }) => {
      await mkdir(screenshots, { recursive: true });
      const context = await browser.newContext({
        viewport: { width, height: 1000 },
        colorScheme,
      });
      const page = await context.newPage();
      const check = await monitor(page);
      try {
        const response = await page.goto("http://127.0.0.1:8480");
        expect(response?.headers()["content-security-policy"]).toBe(
          "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        );
        await expect(
          page.getByRole("heading", { name: "Disruption detected" }),
        ).toBeVisible();
        await expect(page.locator(".role-card")).toHaveCount(5);
        await expect(page.getByRole("table")).toBeVisible();
        for (const title of [
          "Needs attention",
          "Infrastructure roles",
          "Applications",
          "Operations",
          "Platform checks",
        ])
          await expect(
            page.getByRole("heading", { name: new RegExp("^" + title) }),
          ).toBeVisible();
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        ).toBe(true);
        expect(
          await page.locator("[style],style,script:not([src])").count(),
        ).toBe(0);
        await page.screenshot({
          path: path.join(screenshots, `${width}-${colorScheme}.png`),
          fullPage: true,
        });
        const search = page.getByRole("searchbox");
        await search.fill("course-planner");
        const open = page.getByRole("button", {
          name: /course-planner, Serving. Open details/,
        });
        await expect(open).toBeVisible();
        await open.click();
        const dialog = page.getByRole("dialog");
        await expect(dialog).toBeVisible();
        await expect(
          dialog.getByRole("heading", { name: "Accepted deployment" }),
        ).toBeVisible();
        await expect(
          dialog.getByRole("heading", { name: "Identifiers" }),
        ).toBeVisible();
        await expect(
          page.getByRole("button", { name: "Close details" }),
        ).toBeFocused();
        await page.keyboard.press("Escape");
        await expect(dialog).not.toBeVisible();
        await expect(open).toBeFocused();
        await search.fill("absent-fixture");
        await expect(page.getByText("No applications match")).toBeVisible();
        await page.getByRole("button", { name: "Clear filters" }).click();
        await expect(search).toBeFocused();
        await page.getByRole("button", { name: /Show all/ }).click();
        await expect(
          page.getByRole("button", { name: "Show fewer" }),
        ).toBeVisible();
        await page.getByRole("button", { name: "Show fewer" }).click();
        const theme = page.getByRole("button", {
          name: "Color theme: match system",
        });
        await theme.click();
        await expect(page.locator("html")).toHaveAttribute(
          "data-theme",
          "light",
        );
        await page.getByRole("button", { name: "Color theme: light" }).click();
        await expect(page.locator("html")).toHaveAttribute(
          "data-theme",
          "dark",
        );
        await page.reload();
        await expect(page.locator("html")).toHaveAttribute(
          "data-theme",
          "dark",
        );
        await page.getByRole("button", { name: "Color theme: dark" }).click();
        await expect(page.locator("html")).not.toHaveAttribute("data-theme");
        const refreshRequest = page.waitForRequest(
          (request) =>
            request.method() === "POST" &&
            request.url().endsWith("/api/refresh"),
        );
        await page.getByRole("button", { name: "Refresh now" }).click();
        const request = await refreshRequest;
        expect(request.headers()["x-dashboard-refresh"]).toBe("1");
        expect(request.postData()).toBeNull();
        await expect(
          page.getByRole("heading", { name: "Disruption detected" }),
        ).toBeVisible();
        await page.route("**/api/snapshot", async (route) => {
          const response = await route.fetch();
          const data = await response.json();
          if (data.state === "ready") {
            const app = data.applications[0];
            app.slug = "long-fixture-name-".repeat(12);
            app.id = "identifier-".repeat(20);
            app.url = `https://${"long-label".repeat(5)}.${"long-label".repeat(5)}.example.invalid`;
            app.recoveryNote = "<img src=x onerror=alert(1)>";
          }
          await route.fulfill({ response, json: data });
        });
        await page.reload();
        await expect(
          page.getByRole("button", {
            name: /long-fixture-name-.*Open details/,
          }),
        ).toBeVisible();
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        ).toBe(true);
        await page
          .getByRole("button", { name: /long-fixture-name-.*Open details/ })
          .click();
        await expect(page.getByRole("dialog")).toBeVisible();
        expect(
          await page
            .getByRole("dialog")
            .evaluate((element) => element.scrollWidth <= element.clientWidth),
        ).toBe(true);
        expect(await page.locator("img[onerror]").count()).toBe(0);
        await page.keyboard.press("Escape");
        check();
      } finally {
        await context.close();
      }
    });
  }
for (const [scenario, port] of [
  ["healthy", 8481],
  ["outage", 8482],
  ["empty", 8483],
  ["unreachable", 8484],
] as const) {
  test(`${scenario} synthetic evidence and metadata exclusion`, async ({
    page,
  }) => {
    const violations: string[] = [],
      requests: string[] = [];
    await page.addInitScript(() =>
      document.addEventListener("securitypolicyviolation", (event) =>
        console.error("DASHBOARD_CSP:" + event.violatedDirective),
      ),
    );
    page.on("console", (message) => {
      if (message.text().startsWith("DASHBOARD_CSP:"))
        violations.push(message.text());
    });
    page.on("request", (request) => {
      if (new URL(request.url()).origin !== `http://127.0.0.1:${port}`)
        requests.push(request.url());
    });
    await page.goto(`http://127.0.0.1:${port}`);
    await expect(page.locator("#overview")).not.toHaveAttribute(
      "data-tone",
      "pending",
    );
    await expect(page.locator(".role-card")).toHaveCount(5);
    if (scenario === "healthy")
      await expect(
        page.getByRole("heading", { name: "Needs attention" }),
      ).toHaveCount(0);
    if (scenario === "empty")
      await expect(page.getByText("No applications yet")).toBeVisible();
    if (scenario === "unreachable") {
      await expect(page.getByRole("table")).toBeVisible();
      await expect(page.locator(".stale-note").first()).toBeVisible({
        timeout: 25000,
      });
    }
    if (scenario === "outage")
      await expect(
        page.getByRole("heading", { name: "Needs attention" }),
      ).toBeVisible();
    expect(
      (
        await page.request.get(`http://127.0.0.1:${port}/.vite/manifest.json`)
      ).status(),
    ).toBe(404);
    expect(
      (
        await page.request.get(`http://127.0.0.1:${port}/apps/unknown`)
      ).status(),
    ).toBe(404);
    expect(violations).toEqual([]);
    expect(requests).toEqual([]);
  });
}
test("pending collection, reconnect, ETag and retained view state", async ({
  page,
}) => {
  const check = await monitor(page, "http://127.0.0.1:8485");
  const preview = spawn(
    "uv",
    [
      "run",
      "python",
      "-m",
      "openstack_platform.dashboard.preview",
      "--port",
      "8485",
      "--scenario",
      "mixed",
      "--delay",
      "3",
    ],
    { cwd: path.resolve("../.."), stdio: ["ignore", "pipe", "pipe"] },
  );
  await new Promise<void>((resolve, reject) => {
    preview.stdout.on("data", (data) => {
      if (String(data).includes("preview=listening")) resolve();
    });
    preview.on("error", reject);
    preview.on("exit", (code) => reject(new Error(`Preview exited ${code}`)));
  });
  try {
    await page.goto("http://127.0.0.1:8485");
    await expect(
      page.getByRole("heading", { name: "Collecting platform status…" }),
    ).toBeVisible();
    await page.waitForResponse(
      async (response) =>
        response.url().endsWith("/api/snapshot") &&
        response.status() === 200 &&
        (await response.json()).state === "ready",
      { timeout: 12000 },
    );
    await expect(
      page.getByRole("heading", { name: "Disruption detected" }),
    ).toBeVisible();
    const search = page.getByRole("searchbox");
    await search.fill("course-planner");
    const conditional = page.waitForRequest(
      async (request) =>
        request.url().endsWith("/api/snapshot") &&
        Boolean((await request.allHeaders())["if-none-match"]),
    );
    await page.getByRole("button", { name: "Refresh now" }).click();
    await conditional;
    await expect(search).toHaveValue("course-planner");
    await page.route("**/api/snapshot", (route) => route.abort());
    await page.getByRole("button", { name: "Refresh now" }).click();
    await expect(
      page.getByText(/Lost contact with the dashboard service/),
    ).toBeVisible();
    await expect(page.getByRole("table")).toBeVisible();
    await page.unroute("**/api/snapshot");
    await expect(
      page.getByText(/Lost contact with the dashboard service/),
    ).toHaveCount(0);
    await expect(search).toHaveValue("course-planner");
    check();
  } finally {
    if (preview.exitCode === null) {
      const stopped = new Promise<void>((resolve) =>
        preview.once("exit", () => resolve()),
      );
      preview.kill("SIGTERM");
      await stopped;
    }
  }
});
test("reduced motion, forced colors, print and long inventory text", async ({
  page,
}) => {
  const check = await monitor(page);
  await page.emulateMedia({ reducedMotion: "reduce", forcedColors: "active" });
  await page.goto("http://127.0.0.1:8480");
  await expect(page.getByRole("table")).toBeVisible();
  expect(
    await page
      .locator(".card")
      .first()
      .evaluate((element) => getComputedStyle(element).borderTopStyle),
  ).toBe("solid");
  await page.getByRole("button", { name: /grading-service, Failing/ }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  expect(
    await page
      .locator(".drawer")
      .evaluate((element) =>
        parseFloat(getComputedStyle(element).animationDuration),
      ),
  ).toBeLessThan(0.001);
  await page.keyboard.press("Escape");
  await page.emulateMedia({ media: "print", forcedColors: "none" });
  await expect(page.locator(".topbar")).not.toBeVisible();
  await expect(page.locator(".toolbar")).not.toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Infrastructure roles" }),
  ).toBeVisible();
  check();
});
