Here’s what I’d flag so far (definite issues + “likely to bite later”) across the HTML/CSS and all JS you’ve shared.

## Build-breaking / runtime errors

1. **Syntax errors in `gallery.js` rating filtering**: `const filterEmoji = [.filter.rating];` appears in both the semantic-filter path and the standard-filter path. That’s invalid JS and will prevent the module from parsing.
2. **Syntax error in `gallery.js` people-sort de-duplication**: `const uniqueNames = [.new Set(names)];` is invalid JS (looks like it was meant to be `[...new Set(names)]`).

## Logic bugs / behavioural mismatches

3. *Pending initial selection only applies to the first thumbnail that happens to load**: gallery stores `pendingSelection` as a `Set`, but the current logic clears it once it selects a single item. That means if you intended to auto-select multiple IDs, only the first one will ever be selected (depending on fetch timing).
4. **Semantic search threshold UX is a bit inconsistent/confusing**:
  * Search screen converts slider “5..50” to `0.05..0.5` and posts that to `/search`.
  * Gallery’s “similarity” control uses a slider also “5..50” and does the same `value/100` mapping on changes.
  * That’s internally consistent, but the UI copy says “Similarity threshold” and “how similar images must be”, while numerically this behaves like a *minimum score* in the `0.05..0.5` band. If users expect “50% means quite strict”, it may or may not align with how your backend scoring works.
5. **People filter semantics look like AND but naming implies list**: Gallery checks a precomputed `peopleImageIds` set and requires membership. That’s fine, but it means the people filter is effectively whatever backend precomputation returns (and the comment says AND logic). Worth ensuring Search/Backend copy matches this.
6. **Search “validation errors” use `alert()` rather than the app toast pattern**: it’ll feel inconsistent and blocks the UI.

## Lifecycle / cleanup / background activity risks

7. *Duplicates polling can keep running after leaving the screen**: `_scheduleStatusPoll` uses `setTimeout` and checks visibility via `offsetParent`, but I don’t see an explicit `onLeave` cleanup of `_pollTimeout`. That can keep background work alive and occasionally update state at awkward times.
8. **Faces reassessment polling also uses `setTimeout`** and will keep re-scheduling while in progress; ensure it’s always cleared when leaving relevant modes/screens, otherwise you risk “zombie” polls.
9. **Gallery does a good job cleaning up** (unbinds selection/grid handlers, stops refresh interval), but this makes the above polling differences stand out.

## Performance / scalability concerns

10. **People-sort loads faces sequentially per image**: `_loadPeopleNames()` loops images and awaits `GET /images/:id/faces` one by one. With thousands of images this will be very slow and hammer the backend. You probably want batching, concurrency limiting, or a backend endpoint that returns “people names per image” in bulk.
11. **Duplicates fetching is sensibly cached by epoch** (nice), but if computation stays “pending/computing”, the polling cadence is a fixed 2s. That may be fine, but you might want exponential backoff or a server-sent status channel later.
12. **App API wrapper throws only `status/statusText`** (no body), which can hide useful server error details when debugging.

## API / contract / edge-case risks

13. **`App.api()` always sends `Content-Type: application/json` even for GET**. Usually harmless, but it can trigger CORS preflights in some deployments or confuse certain servers/middleware.
14. **Search assumes `/status` is reachable and defaults face detection to enabled on failure**. That’s a reasonable fallback, but it can lead to UI offering people filtering when the backend can’t support it.
