import { expect, test } from "@playwright/test";

// Runs against the real stack: the demo shop on GATEWAY_URL and ServScope on SERVSCOPE_URL.
const GATEWAY = process.env.GATEWAY_URL ?? "http://localhost:8080";
const API = process.env.SERVSCOPE_URL ?? "http://localhost:8000";

const randomHex = (bytes: number) =>
  Array.from(crypto.getRandomValues(new Uint8Array(bytes)), (b) => b.toString(16).padStart(2, "0")).join("");

test("a declined checkout can be found by its ID and followed to the log line that explains it", async ({ page, request }) => {
  // 1. A customer's card is declined. We pick the trace ID so we can find it again.
  const traceId = randomHex(16);
  const checkout = await request.post(`${GATEWAY}/checkout`, {
    headers: { traceparent: `00-${traceId}-${randomHex(8)}-01` },
    data: { items: [{ sku: "mug", qty: 1 }], card_token: "tok_demo_declined" },
  });
  expect(checkout.status()).toBe(402);

  // 2. Wait until all four services have reported. (Not a span count: a declined order has a
  //    fifth server span when orders gives the stock back, and it may arrive last.)
  await expect
    .poll(async () => {
      const r = await request.get(`${API}/api/traces/${traceId}`);
      if (!r.ok()) return "";
      const body = await r.json();
      const reported = new Set(body.spans.filter((s: { kind: string }) => s.kind === "server").map((s: { service: string }) => s.service));
      return [...reported].sort().join(",");
    }, { timeout: 30_000 })
    .toBe("gateway,inventory,orders,payments");
  await expect
    .poll(async () => (await (await request.get(`${API}/api/logs?trace_id=${traceId}`)).json()).logs.length, { timeout: 30_000 })
    .toBeGreaterThan(1);

  // 3. Find it by its ID, the way you would from a customer's error report. (Not by
  //    scrolling the trace list: the demo's steady traffic keeps pushing new traces on top.)
  await page.goto("/");
  await page.getByRole("searchbox", { name: /Find a trace by its 32-character ID/ }).fill(traceId);
  await page.keyboard.press("Enter");

  // 4. The waterfall shows the whole request across four services.
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("gateway POST /checkout");
  const waterfall = page.getByRole("region", { name: "Span waterfall" });
  for (const service of ["gateway", "orders", "inventory", "payments"]) {
    await expect(waterfall.getByRole("button", { name: new RegExp(`^${service} `) }).first()).toBeVisible();
  }

  // 5. Click the card authorisation step: its log line says why the order failed.
  await waterfall.getByRole("button", { name: /payments authorize_card/ }).click();
  const details = page.getByRole("complementary", { name: "Selected span" });
  await expect(details.getByText(/card declined for order/)).toBeVisible();
  await expect(details).not.toContainText("tok_demo"); // the card token never reaches telemetry

  // 6. And every service's lines for this request are one click away.
  await details.getByRole("link", { name: "All logs for this trace" }).click();
  await expect(page.getByRole("region", { name: "Log lines" })).toContainText("card declined, releasing stock");
});

test("the overview shows every service and the calls between them", async ({ page, request }) => {
  await request.post(`${GATEWAY}/checkout`, { data: { items: [{ sku: "tea", qty: 1 }] } });
  await page.goto("/");
  const services = page.getByRole("region", { name: "Services" });
  for (const name of ["gateway", "orders", "inventory", "payments"]) {
    await expect(services.getByRole("link", { name, exact: true })).toBeVisible({ timeout: 30_000 });
  }
  await expect(page.getByRole("img", { name: /orders calls payments/ })).toBeVisible();
});

test("an unknown trace ID gets a helpful page, not a crash", async ({ page }) => {
  await page.goto(`/traces/${"0".repeat(31)}1`);
  await expect(page.getByText("Trace not found")).toBeVisible();
});
