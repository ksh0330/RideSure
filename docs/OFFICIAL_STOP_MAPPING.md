# Historical occurrence → official stop mapping

Phase 2A adds an optional mapping layer. The historical `Stop` nodes remain
name-based and retain their IDs; `StopOccurrence`, `NEXT`, and congestion data
are unchanged. TAGO route stops are imported as separate official `Stop` nodes
and ordered `RouteStopStaging` nodes. An approved match is recorded as
`(StopOccurrence)-[:VERIFIED_OFFICIAL_STOP]->(official Stop)`.

## Evidence and status

`official_stop_mapping.py` needs an explicit historical line/pattern and an
official TAGO route ID. `--route-binding-source` records where the route ID was
checked against the historical line; the program cannot infer line identity
from an opaque TAGO ID. If TAGO numbers a round trip continuously across a
direction change, the whole ordered sequence is used. If node orders restart
in each direction, select one with `--direction-code`. A missing direction
code is kept unknown, not invented.

- `EXACT`: at least three historical occurrences match the complete selected
  official sequence after deterministic presentation normalization. Each
  occurrence maps by its own position, including repeated names.
- `SEQUENCE_MATCH`: the full sequences differ, but a historical occurrence is
  matched to one official position in **every** maximum-length monotonic
  one-to-one alignment of equal normalized names. Official-side insertions
  and renamed stops may remain gaps. At least three names must match across
  the route before any partial mapping is verified.
- `AMBIGUOUS`: an occurrence has multiple optimal official positions, can be
  skipped in another optimal alignment, or has unselected competing route
  directions. No edge is written.
- `UNMATCHED`: no safe normalized-name alignment, including semantic renames.
  No edge is written.

Name normalization uses Unicode NFKC and folds whitespace, periods, commas,
or middle dots **only between Hangul syllables**. Digits, numeric punctuation,
brackets, qualifiers, and word order remain meaningful. There is no fuzzy
matching or semantic alias table. A single matching name, even with an
official node ID, does not create a mapping. The preview records each
occurrence's possible official orders across optimal alignments, whether the
occurrence can be skipped, and whether its approved name was raw-exact or
presentation-normalized. Changed route segments may remain unmapped until
their physical identity can be checked separately.

The edge records mapping method/status, normalized name and match kind,
official source, route ID, node ID, direction/order, staging ID, route-binding
source, and coordinate source.
Coordinates are read through this edge only for `EXACT`/`SEQUENCE_MATCH`;
they are never copied onto the historical name-based `Stop`. This mapping is
not yet used by the routing service or frontend.

## Prepare and inspect B1

The national bus-stop CSV is **not required** for this mapping: TAGO route
stops already include official node IDs, ordered route positions, and
coordinates. The CSV alone cannot establish which repeated historical
occurrence corresponds to which physical stop, because it has no route order.
The required external input is the correct official TAGO B1 route topology
plus evidence that its city/route ID belongs to the intended B1 line.

One way to import it, after setting `DATA_GO_KR_SERVICE_KEY` locally:

```powershell
.\.venv\Scripts\python.exe .\public_data.py fetch-tago --city-code <official-city-code> --route-id <official-route-id> --import-to-neo4j
```

Or import a manually saved original TAGO route-stop JSON response, without an
API key:

```powershell
.\.venv\Scripts\python.exe .\public_data.py import-tago-json --path <tago-route-stop-response.json>
```

Find the B1 historical `pattern_id` with a read-only Neo4j query:

```cypher
MATCH (:Line {name: 'B1'})-[:HAS_PATTERN]->(p:RoutePattern)
RETURN p.pattern_id, p.terminal_description;
```

Then preview before applying. The binding source should identify the official
route listing or operator record used to check the ID:

```powershell
.\.venv\Scripts\python.exe .\official_stop_mapping.py plan --line-name B1 --pattern-id <historical-pattern-id> --official-route-id <official-route-id> --route-binding-source "<source and check date>"
.\.venv\Scripts\python.exe .\official_stop_mapping.py apply --line-name B1 --pattern-id <historical-pattern-id> --official-route-id <official-route-id> --route-binding-source "<source and check date>"
```

For a read-only preview directly from a saved TAGO response, add
`--tago-json <route-stop-response.json>` to the `plan` command. This does not
require an official-data import. `apply` does not accept that option.

Add `--direction-code <official-code>` to both commands if needed. Review the
preview's status counts and per-occurrence evidence before `apply`. For B1,
inspect both historical occurrences of `오송역2.3.4`, current direction/order,
official node IDs, and coordinates. The program never inserts current-only
official stops into the historical sequence. Reapplying an unchanged plan
uses `MERGE` and preserves one edge per matched occurrence; an existing edge
to another official stop is rejected for manual review.
