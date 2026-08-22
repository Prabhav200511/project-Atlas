# Three-minute UPS-01 connected demo

Use a freshly seeded `Atlas Synthetic Demo` project. Say “synthetic” for documents, shipments, schedule values, costs, and results. The dashboard calls the story “UPS-01”; select `UPS-A`, the equipment tag in the seeded source documents.

## Before recording

1. Run `./scripts/start_demo.sh` and wait for the dashboard.
2. Confirm `http://localhost:8001/ready` reports API, database, and Qdrant ready.
3. In the project selector, choose the newest `Atlas Synthetic Demo`.
4. Click **Reset Demo**, confirm the dialog, and wait for the success message.
5. Keep [evaluation/latest.md](../evaluation/latest.md), [evaluation-results.svg](backup/evaluation-results.svg), and [schedule-chain.svg](backup/schedule-chain.svg) open as backups.

## Timed walkthrough

| Time | Exact clicks/action | What to say |
| --- | --- | --- |
| 0:00–0:20 | Click **Project overview**. Point to the synthetic badge, document count, and API status. | “Atlas connects synthetic EPC evidence by project and equipment. AI suggestions remain separate from human-approved records.” |
| 0:20–0:40 | Click **Equipment thread** → select **UPS-A** → **Load thread** → **Open evidence drawer**. | “This is the UPS-01 connected story; the seeded source tag is UPS-A. The thread links current documents, findings, RFIs, shipment, schedule, commissioning, and evidence without crossing projects.” |
| 0:40–1:05 | Click **Compliance findings** → choose `UPS_Specification.md` and `UPS-002_VoltEdge_UPS-A.md` → **Run comparison**. On the voltage finding, click **Evidence**, close it, then **Approve**. | “The deterministic rule detects the synthetic 480/277 V versus 415/240 V deviation. Evidence is cited; approval is a human action.” |
| 1:05–1:35 | Click **Impact Chain** → **Retry inputs** if needed → **Run connected flow**. Point to delay, float, readiness, and the three scenarios. Click **Evidence chain** and close it. Choose one scenario → **Approve**. Then open **Mitigation calculator**. | “Atlas resolves the equipment, shipment, CPM impact, readiness, and three deterministic mitigation scenarios. In the counterfactual mitigation calculator, user-supplied assumptions are applied deterministically; values are not predictions or quotations.” |
| 1:35–1:58 | Click **Knowledge / RFI** → **Knowledge chat**. Ask: `What is the minimum UPS-A battery autonomy?` → **Ask Copilot**. Point to the cited page/section. | “The answer is generated only from selected project evidence and important claims carry citations. Insufficient evidence is returned instead of a guess.” |
| 1:58–2:18 | Click **RFI match**. Paste: `Please verify the clear aisle needed in front of the modular UPS and its service/bypass section so technicians can isolate and maintain it.` → **Find previous matches**. | “RFI-003 should appear as a possible previous match with its prior 900 mm resolution. Atlas does not automatically declare a duplicate.” |
| 2:18–2:38 | Click **Commissioning readiness** → select **UPS-A** → **Calculate readiness**. Point to the visible weighted rules. Select `UPS_Procedure_Template.md` → **Retrieve procedure**. | “Readiness and pass/fail are deterministic. Engineer observations create controlled test records and failed criteria create NCRs.” |
| 2:38–2:52 | Click **Synthetic supply-chain demo** → select the UPS shipment → **Analyze risk**. Point to the **Synthetic demo data** badge and alternative card. | “No live carrier, AIS, vendor, ERP, or position feed is connected. This is synthetic milestone analysis; live logistics integrations are roadmap only.” |
| 2:52–3:00 | Click **Evidence Dashboard** → select **UPS-A** → **Load evidence**. | “Atlas closes the loop with revision, approval, citation, NCR, mitigation, and audit visibility. The prototype is evidence-led, not autonomous approval.” |

## Backup if an API step fails

1. Do not present stale UI state as a live response. State: “The live API step is unavailable; I’m switching to a labelled static backup from the synthetic evaluation.”
2. Open `docs/backup/evaluation-results.svg` for measured evaluation values or `docs/backup/schedule-chain.svg` for the planted delay chain. These are labelled static assets, not dashboard screenshots.
3. Open `evaluation/latest.md` for the exact current metrics and `docs/ARCHITECTURE.md` for the implemented/roadmap boundary.
4. If recovery time remains, run `curl -fsS http://localhost:8001/ready`. Restart only the failed dependency with `docker compose up -d postgres qdrant`; then restart FastAPI with `python3 -m uvicorn app.main:app --port 8001` and use the page’s retry action.
5. If the project state is inconsistent, return to the dashboard, select the project, click **Reset Demo**, and retry. This resets only project-scoped synthetic shipment events and preserves documents.

Do not claim official-standard compliance, historical prediction accuracy, live tracking, production scalability, hours saved, or business ROI.
