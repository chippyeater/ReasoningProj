# Frontend Notes

This frontend is intentionally minimal.

- Left: case text + question + submit
- Right: entities + dynamic reasoning view
- Dynamic view is chosen by `recommended_view` from backend response:
  - `conflict_compare`
  - `timeline_reasoning`
  - `hypothesis_board`

API endpoint is configured in `src/api.ts` with default `http://localhost:8000`.

Optional env:

```bash
VITE_API_BASE=http://localhost:8000
```
