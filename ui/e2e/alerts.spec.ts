import { expect, test, type APIRequestContext } from "@playwright/test";

// The Milestone 5 story in a real browser against the real stack: break payments from the
// Demo scenarios screen, see the alert, and follow its evidence to the failing request.
const API = process.env.SERVSCOPE_URL ?? "http://localhost:8000";
const TOKEN = process.env.SERVSCOPE_ADMIN_TOKEN ?? "";

interface Incident {
  id: number;
  rule_name: string;
  service: string;
  opened_at: string;
}

async function newIncident(request: APIRequestContext, since: number): Promise<Incident | undefined> {
  let incidents: Incident[];
  try {
    // A poll can land just as the server closes an idle keep-alive connection; that is the
    // network, not an answer, so it counts as "not yet" and the poll asks again.
    const r = await request.get(`${API}/api/incidents?state=open`);
    ({ incidents } = (await r.json()) as { incidents: Incident[] });
  } catch {
    return undefined;
  }
  return incidents.find(
    (i) => i.rule_name === "Checkout errors" && i.service === "gateway" && new Date(i.opened_at).getTime() >= since,
  );
}

test("a payments timeout raises an alert whose evidence leads to the failing request", async ({ page, request }) => {
  test.skip(!TOKEN, "needs SERVSCOPE_ADMIN_TOKEN (the stack must run with failure scenarios on)");
  test.setTimeout(300_000);
  const since = Date.now() - 5_000; // a little slack for clock differences

  // 1. Unlock admin and start the scenario from its card.
  await page.goto("/scenarios");
  await page.getByRole("button", { name: "Unlock admin" }).click();
  await page.getByPlaceholder("Admin token").fill(TOKEN);
  await page.getByRole("button", { name: /^Unlock$/ }).click();
  const card = page.getByRole("article", { name: "Payments timeout" });
  await card.getByRole("combobox").selectOption("5");
  await card.getByRole("button", { name: "Start Payments timeout" }).click();
  await expect(page.getByRole("region", { name: "Running scenario" })).toContainText("Payments timeout is running");

  try {
    // 2. The alert fires (settle 30 s + for 30 s + up to one 15 s cycle, after errors start).
    let incident: Incident | undefined;
    await expect
      .poll(async () => (incident = await newIncident(request, since)) !== undefined, { timeout: 240_000, intervals: [3_000] })
      .toBe(true);
    if (!incident) throw new Error("unreachable");

    // 3. The Alerts screen shows it; the badge counts it.
    await page.goto("/alerts");
    const alert = page.getByRole("article", { name: "Checkout errors on gateway" });
    await expect(alert).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Main" }).getByLabel(/open incident/)).toBeVisible();

    // 4. Its page tells the story so far and links the evidence.
    await alert.getByRole("link", { name: "Investigate" }).click();
    await expect(page).toHaveURL(new RegExp(`/alerts/incidents/${incident.id}$`));
    const timeline = page.getByRole("region", { name: "Timeline" });
    await expect(timeline).toContainText("Alert fired");
    await expect(timeline).toContainText("error rate above 10% over 2 min");

    // 5. One click to a failing request, where the cause is spelled out.
    await page.getByRole("link", { name: /Open a failing trace/ }).click();
    const waterfall = page.getByRole("region", { name: "Span waterfall" });
    await expect(waterfall.getByRole("button", { name: /^orders POST \/orders.*error/ })).toBeVisible();
    await waterfall.getByRole("button", { name: /^orders POST \/orders/ }).click();
    const details = page.getByRole("complementary", { name: "Selected span" });
    await expect(details).toContainText("payments_timeout");
    await expect(details.getByText(/payments timed out after 2.0s, voiding the charge/)).toBeVisible();
  } finally {
    // Never leave the shop broken for whatever runs next.
    await request.post(`${API}/api/scenarios/stop`, { headers: { Authorization: `Bearer ${TOKEN}` } });
  }
});

test("scenario controls stay locked without the admin token", async ({ page, request }) => {
  const config = await (await request.get(`${API}/api/config`)).json();
  test.skip(!config.scenarios_enabled, "failure scenarios are switched off on this stack");
  await page.goto("/scenarios");
  await page.getByRole("button", { name: "Start Payments timeout" }).click();
  await expect(page.getByRole("dialog", { name: "Unlock admin" })).toBeVisible(); // asks, doesn't start
  const r = await request.post(`${API}/api/scenarios/payments_timeout/start`);
  expect(r.status()).toBe(401);
  expect((await (await request.get(`${API}/api/scenarios`)).json()).active).toBeNull();
});
