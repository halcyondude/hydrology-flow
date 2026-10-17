---
name: project-scope-generic-tool
description: hydrology-flow is a general-purpose hydrology tool; Worcester County / Cook's Pond is only the proving example
metadata:
  type: project
---
hydrology-flow must be a general-purpose tool. Worcester County, MA and Cook's Pond are the reference example only.
The area of interest must be selectable by ZIP code, city/state, one or more counties, or a whole state.

**Why:** Matt said so on 2026-10-17: "anything about the geographic specifics is just to prove it out."
**How to apply:** Keep geography out of code. Put it in data profiles (e.g. `profiles/worcester-ma/`). Reject any PR that hard-codes coordinates, COMIDs or station IDs in `src/`. Design for scale-out (multi-county, statewide) via tiling or chunking by HUC.
Related: [[project-cooks-smith-ponds]], [[project-priority-sediment-wq]]
