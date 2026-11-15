# Change summary (human-readable)

We fixed two things on the hands-off phone demo.

## 1. How the call sounds after you say “apply the fix”

**Before:** dead air, then “are you still there?”, then messy double goodbyes.

**Now:**

1. It says a short “okay, on it…”
2. Applies the fix quietly
3. Says “it’s fixed — anything else?”
4. If you’re done, it says goodbye **once** and hangs up

## 2. Same demo on Mac and Windows

**Before:** config pointed at a Mac-only path and the launcher was a bash script.

**Now:** one portable setup. Same demo from either laptop:

```bash
# Mac / Linux
./demo/run_hands_off.sh

# Windows
demo\run_hands_off.cmd

# Either OS
python demo/run_hands_off.py
```

Your phone still gets the FaceTime call. Your laptop (Mac or Windows) shares the screen and runs the agent.
