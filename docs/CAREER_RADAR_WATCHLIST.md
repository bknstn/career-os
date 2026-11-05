# Career Radar Watchlist

Watchlist saves applied Explore filters so you can return to a company board,
role, specialty, skill, or text-search slice. A slice needs at least one of
those filters. It stores at most 20 unique slices in this browser; changing the
comparison period does not create another copy. Saved jobs remain in the
separate shortlist.

## Current inventory

Each card shows the current saved count of active postings that match its
filters. Current inventory uses enabled sources. Freshness comes from the
configured source roster and its last successful scan. A company choice means
that company's configured board, not necessarily every vacancy. If a board is
not enabled or has never had a successful scan, its count is shown as
unavailable rather than as a confirmed zero.

## Historical evidence

Choose a 30-, 90-, or 180-day period. Comparisons describe two adjacent windows
from immutable saved scans, not change since the last time you opened
Watchlist. The comparison uses the API's selected metric and existing quality
gates. It shows a direction only when that exact metric has a passing quality
state and numeric delta. A selected role or skill that lacks sufficient data
stays insufficient; another metric is not substituted. Single-company filters
keep the multi-company evidence requirements.

Historical windows come from the registered panel, which can include boards
that are disabled in the current inventory. A company filter narrows the
historical panel to that board. Current text search checks titles, descriptions,
company names, and source keys; historical text matching checks titles and
descriptions. Use the company filter to follow a specific board.

Historical window counts and denominators are shown separately from the
current posting count when the API provides them. Missing history is not zero.
Posting observations do not establish hires, job closures, new openings, or
employer intent, and Watchlist does not generate a career verdict.

## Browser storage

Watchlist stores a bounded, versioned browser-local list of canonical filters
and display labels. When you open Watchlist, each saved filter is sent with a
read-only snapshot request to load its inventory and evidence. The server does
not persist Watchlist entries or create an account. Malformed or unsupported
stored data is left unchanged and reported in the interface. Storage write
failures are also reported; changes that cannot be saved last only for the
current page. Removing a Watchlist slice does not remove saved jobs.
