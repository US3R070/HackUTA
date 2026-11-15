import asyncio
import os

os.environ["ESCALATION_MODE"] = "live"

from escalation_mcp import session as sess  # noqa: E402


async def main():
    sess.configure_runtime()
    res = await sess.start_escalation(
        title="Training run crashed: NaN loss at step 4200",
        summary="Loss became NaN at step 4200. Learning rate 5.0 looks too high.",
        failure_type="nan",
        severity="HIGH",
        proposed_fix="Lower lr to 3e-4 and resume from checkpoint.",
        share="screen",
    )
    print(res)
    iid = res["incident_id"]

    # narrate on the overlay while you're on the call
    for step in ["Reading traceback", "Checking config.yaml (lr=5.0)", "Drafting fix: lr -> 3e-4"]:
        await asyncio.sleep(6)
        await sess.report_action(iid, step, status="done")
        print("overlay:", step)

    print("\nOn the call now. Ctrl+C to end.")
    try:
        await asyncio.Event().wait()
    finally:
        await sess.end_escalation(iid, "aborted", "test ended")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass