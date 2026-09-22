# SOP 02 — Daily analyst use

**Purpose.** The working loop: material in, reviewed, cleaned up, written up,
out.

**Prerequisite.** A running instance ([SOP 01](01-install.md)) and an account.

**How to use this.** Read it once end to end, then do it on the exercise set
in `docs/exercise/` before you touch anything real. Those eight documents are
invented and go from an empty instance to a finished assessment, so every
mistake is free.

---

## The loop

```
  document in  →  review what was proposed  →  merge duplicates
       ↑                                              ↓
   export  ←  write the report  ←  fix the confidences
```

Five steps. The platform never skips one for you: **nothing a model proposes
reaches your case file until you accept it.**

---

## 1. Get the material in

**Documents** tab. Two ways in:

- **Drop a file.** PDF, image, `.docx`, `.txt`. Scans are OCR'd.
- **Paste text.** The box underneath takes an intercepted transmission, a
  forwarded message, a transcript read out over the phone — anything that
  arrived as words rather than as a file. Name it if you want; the first line
  is used if you do not.

Status moves *Waiting* → *Reading* → *Read*. On a Pi with a local model,
*Reading* can take a few minutes for a long document; nothing in the app waits
on it, so carry on working.

**A document does not have to belong to anything yet.** The inbox exists
precisely so material can sit there while you work out what it is. File it to
a report or a record later, or leave it.

### Recording what the model missed

It misses things — a reporter's byline, a photographer's credit, a company
named once in passing. Open the document and you can record them without
leaving the page:

- **Select the name in the text.** A button appears under the selection with
  the name in it. Press it and the normal entity form opens with the name filled in; the sentence it came out of is kept as the evidence.
- **Press "+ Add entity"** for what the document implies but never spells out.

Either way you stay on the document — there is usually more than one — and
what you added appears on the right under "added by hand from this document". They are ordinary entities, and **Review → From: Added by hand** finds every one of them later.

### Keeping the whole document as a Record

Some documents matter as documents: an insurance letter, a bank statement, a
vehicle registration printout. Open it and press **Save as Record**.

- **Name, kind, date and issuer.** The name starts as the document's title.
  The date is the one printed on the document, not today's.
- **Link to.** Everything already accepted from this document is listed and
  ticked. Untick what doesn't belong, and search to add anything else. Each
  link reads "*entity* mentioned in *Record*".
- **Full text.** Opens for editing if you want to fix an OCR mistake first. The
  Record keeps its own copy, so reading the document again later won't change
  it.
- **Move the original file onto the Record** is ticked by default, which takes
  the document out of the inbox and onto the Record's attachments.

A document can only be saved as a Record once; afterwards its page links to
the Record instead. Records can also be made by hand with **+ New Entity →
Record**, or imported from CSV like any other type.

### If a document says *Read* but proposed nothing

Usually the model was down when it was read. Open it and press **Read
again**, or — after an outage that hit several — use **Read empty ones
again** on the inbox toolbar, which re-queues exactly the documents that came
back empty. Both leave decisions you have already made alone.

## 2. Review what was proposed

**Review** tab. Two queues:

- **Extraction** — records and relationships proposed from documents, from the
  signal pass, or by the assistant when you asked it to.
- **Correlation** — "these two records look like the same thing".

Every card carries its evidence: which document, which sentence, which rule
fired, what it matched on. **Read the evidence, not the confidence score.** A
number is not a reason.

### When the queue is enormous

It happens after a bulk import or a new feed: hundreds of similar records
arrive and the correlation pass proposes pairs between all of them.

Filter by similarity first. **Below 80%** is the weak tail, and it is where a
bulk dismiss is safe — those pairs are alike in wording rather than in fact.
Tick rows, or use **Select all shown**, then *Not a match*. If the filter
matches more than fits on screen, the bar offers to act on the whole set and
tells you the number twice before it does anything.

Work the strong end by hand. Above 90% is where the real duplicates are, and
that is the part worth your attention.

If a feed caused it, fix the feed too — see [SOP 03](03-admin.md) — or it will
happen again on the next poll.

### Work entities before relationships

Use the **Entities only** filter first, then switch to **Relationships only**.

This is not fussiness. A relationship can only be accepted once both ends
exist as records, so working in the other order means repeatedly stopping to
create something. The queue is sorted entities-first by default; the filter
makes it a choice.

Also useful: **Every document** narrows to one source. Eight uploads produce
one long queue otherwise, and reviewing a queue document by document matches
how the material actually arrived.

### What to do with a card

- **Accept** — it goes into the case file. Relationships arrive as
  *possible*; you will fix that in step 4.
- **Dismiss** — it does not. Dismissing is not a failure, it is the job. A
  queue you accepted everything from was not reviewed.
- **Edit before accepting** — change the name, the type, or which records the
  two ends point at. A proposal with the right idea and the wrong spelling is
  worth fixing, not throwing away.

If a proposal names a record that does not exist yet, the card offers to
create it as part of accepting. You do not need to go and make it first.

### The signal pass, if you have it on

With `LINK_SIGNALS_ENABLED=true`, a periodic pass proposes links from records
you have already entered — shared surname, shared address, shared contact
detail, co-mention in the same report. No model involved; it is SQL, and the
evidence names exactly what it matched.

Treat the low-confidence ones with suspicion by design. Two people sharing a
radio frequency on a national calling channel are not associates; two people
sharing an unlisted mobile number probably are. The confidences reflect that,
and so should you.

## 3. Merge the duplicates

Extraction produces duplicates. It is not a defect: the same person is named
four ways across six documents, and a model reading one document cannot know
what another one called them.

Two ways in:

- **Correlation queue** → **Merge into one** on a pair. The two buttons
  beside it are real answers, not consolation prizes: *Same, but keep both*
  records that you agree they are the same subject and want them kept apart
  anyway, and *Not a match* says the pass was wrong.
- **Entities** → **Select to merge** → tick two or more rows → **Merge
  selected**.

The dialog costs the merge before you commit: how many relationships, report
links, attachments and contacts move, which blank fields get filled from the
losers, and whether an edge has to be dropped because a record cannot be
related to itself. **Read it.** There is no one-click undo.

Pick the survivor deliberately — it defaults to the record carrying the most,
which is usually right. Blank fields on the survivor are filled from the
losers; fields it already has are never overwritten.

### The split address

A model reading "1140 Rennard Way, Kettleburn 74101" sometimes files three
Locations: a street, a town, a postcode. Select all three, merge them, and
**rename the survivor in the same step** — the dialog has a name field — to
the whole address. Do not merge them and then go and edit the name; that is
two trips for one thought.

Losers are archived with a pointer to where they went, not deleted. Old
exports and audit entries still resolve.

### Clearing out noise, if you are an admin

Merging is for two records that are the same thing. Some records are not
anything — "ATTACHMENT A", "Page 2 of 4", whatever the model made of a page
header. Archiving hides those from one list and leaves every row in the
database.

Admins can **Delete** them for good, from a record's own page or by ticking
several in **Select to merge** and pressing Delete beside Merge. The dialog
counts exactly what goes and makes you type DELETE. It refuses outright if a
**confirmed** report cites the record — archive that one, or unlink it from
the report first. A draft citing it is only a warning: the draft keeps its
text and loses the link.

There is no undo. Only a restore from a backup brings it back.

### When the two records are different kinds

Extraction sometimes files one company as both a Person and an Organization.
Select them both anyway: the dialog opens with a checkbox saying how many kinds
are in the selection, and until you tick it nothing is merged. Tick it and a
**Keep it as** picker appears — choose the kind the merged record should be,
and the preview names exactly which fields the other record loses, because each
kind's fields live in their own place and a date of birth has nowhere to go on
an Organization.

Everything else moves regardless of kind: the name, relationships, report
links, attachments, contacts, the description. Only the kind-specific fields on
the odd record out are given up, and you are told which ones before you
commit.

### Alignment, and where factions live

**Alignment** — Friendly, Neutral, Unknown, Hostile — is your own read on
whose side a person, organization, source or vehicle is on. It is never
proposed by the model, because it is a judgement about the case rather than
something a document said.

**Named groups are records, not a dropdown value.** Make "Iron Horse Militia"
an Organization, give *it* an alignment, and link people to it with
`member_of`. That way the group has a place to keep its own reporting, and a
member can be aligned differently from the group — which is the situation you
most need to be able to write down.

A source's alignment is separate from its A–F reliability. Hostile and grade A
is a real and useful combination.

**Anything you mark Hostile gets its name shown in red** — in the tree, on its
own page, in the relationship list of everything connected to it, in search,
and on the network. Nothing else is coloured, so that the colour keeps
meaning something.

**Locations get an environment instead**: Permissive, Semi-permissive,
Non-permissive, Denied, Unknown. Ground does not take a side; it is more or
less workable. Denied is not a stronger word for non-permissive — it means not
an option at all.

If you are coming from an older build, Faction became Alignment and lost its
*Family* value; kinship belongs on a `family_of` relationship, where it can
also say how. Records that already had Family keep it, shown as retired.

## 4. Fix the confidences

Everything from extraction and the signal pass arrives as **possible**. That
is the honest default for a machine's opinion. The point of review is that
some of those become probable and a few become confirmed as you corroborate
them.

On any record, press **Edit** on a relationship to change its type,
confidence, discovery date and notes in place. Use the notes: *why* something
is only possible is exactly what you need when you come back to it in three
weeks.

A rough scale that works:

| | Means |
|---|---|
| **possible** | One source said so, or a machine inferred it. Nothing corroborates it. |
| **probable** | More than one source, or one source you trust with something to lose. |
| **confirmed** | You would put it in a report and defend it. |

The exercise is built around this: the roster identification is *probable* and
is deliberately the weakest link in the chain, which the final assessment says
out loud.

## 5. Write it up

**Reports** tab → **+ New Report**.

- **@-mention records** as you write. The link is made both ways — the report
  lists the record, and the record lists the report.
- **Credibility rating** is about the sourcing. **Precedence** (Flash /
  Immediate / Priority / Routine) is about how fast somebody needs to read it.
  They are different axes: a Flash report can be poorly sourced, and saying so
  is the honest thing to do.
- **Status** — draft or confirmed. Drafts you have not touched for a fortnight
  show up on the dashboard, because a forgotten draft is the most common way
  work gets lost.

Write what the file supports and say where it is thin. The assessment in the
exercise ends with a recommendation to corroborate the weakest link before
acting on it; that is the shape to copy.

## 6. Get it out

- **A report on its own** — **Export PDF** on the report's page.
- **Everything about one record** — **Export** on the record's page, which
  offers three shapes: an *Organisation report* (structure, membership and
  holdings), a *Target package* (everything on file), or a *Plain dossier*
  (the record and what it connects to).
- **The last N days** — **Executive summary** on the dashboard.
- **The whole instance** — Admin settings → Backup & Restore. That is a
  backup, not a document; see [SOP 03](03-admin.md).

Exported PDFs carry whatever header and footer you set in `.env`
(`PDF_HEADER_LABEL`, `PDF_FOOTER_NOTE`). Set them before you send anything to
anybody.

---

## Moving things around the network

Drag any dot to move it. It stays where you put it, marked with a dashed ring,
while everything else shuffles to make room — useful for pulling two records
apart to see what actually runs between them.

- **Double-click** a held dot to let the layout have it back.
- **Refit** releases every held dot and re-runs the layout from scratch.
- A single click opens a summary, not the record — see below.

The picture keeps drifting gently after it settles. It stops on its own when
the tab is in the background or the network is scrolled out of view, and it
does not drift at all if your system is set to reduce motion.

## Click a dot for a summary

A single click on any dot in a network opens a card beside it: what the record
is, its alias and alignment, the two or three fields that identify it, how many
links and reports it has, and its nearest connections with the confidence on
each.

- **A connection on the card** opens that record's card, so you can walk a
  chain without leaving the picture.
- **Open record** goes to the full record when you want it.
- **Actions ▾** is the same menu as a right-click.
- Escape, or a click anywhere else, closes it.

This is for reading the shape of the file: "who is that one?" answered in
place, instead of two page loads and losing the arrangement you just made.

## Closing a form

Click outside a dialog, or press Escape, and it closes. Once you have typed
something into it, both ask first. Selecting text with the mouse and releasing
outside the box no longer closes anything.

## Right-click a record

Anywhere a record's name appears — the tree, a card, a node in the network, a
map popup, a report's linked list — right-click it for a menu. The record's own
page has the same menu on its **Actions** button.

| Item | Use it when |
|---|---|
| **Look for links** | You want the model's opinion on what this record connects to. Proposals go to Review; nothing is added to the case until you accept it. |
| **Ask the assistant about this** | You want the file's answer on one subject without typing the question. |
| **What is missing on this record?** | You are deciding what to chase next. |
| **Find similar records** | You suspect a second file on the same person, or an alias. Shows a score; flag a pair as a duplicate, or go straight to a merge. |
| **Add a relationship / Write a report about this** | The next thing you were going to do anyway, without navigating back. |
| **Select to merge** | Starts the merge with this record already picked. |
| **Export a dossier / Show on the map / Copy name / Copy record ID** | Housekeeping. |

The first four need Ollama. With it switched off they stay in the menu, greyed
out, so you can see what you would get by turning it on.

## Reading the Entities page

The tree is names; the network beside it is shape.

**The tree.** One branch per type, folded how you leave it. Each row shows the
record's relationship count. Search and the type filter drive both panes.

**The network.** A dot per record, a line per relationship, **graded by how
many relationships each record has** — so the thing everything points at is
the big bright dot. Hover to name it and light its neighbours; click for a
summary card. *Hide unconnected* is on by default, because a rim of orphans
makes the part that matters smaller.

Use it to ask "what is this file about?" before you ask about any particular
record. In the exercise, the answer is visible the moment the layout settles.

## The guided debrief

**Reports → Start a debrief** walks an interview in five steps and files a
report at the end. It creates no "debrief" object of its own: what comes out
is a report and some entities, the same as if you had written them by hand.

### When the source cannot give you an address

They often can't, and they can still point at the roof. On **When & where**,
press **Point at it on the map** and let them show you. Name the place in
their words — "the yard behind the grain store" — and it becomes a Location
record with real coordinates when you file the report.

Nothing is written until you file. A debrief you abandon halfway leaves no
half-named Location behind.

This is the case the map earns its keep for, so it is worth having the area
downloaded before the conversation rather than during it.

## The map

**Basemap** at the top of the Map page picks which tiles to draw. A source
marked *downloaded* has offline coverage; **Downloaded only** draws from that
alone, which is what an analyst with no connection sees. Outside a downloaded
area the map is blank rather than broken — there is genuinely no imagery
there. Admins add sources and download areas (see [SOP 03](03-admin.md)).

Clicking empty ground still creates a Location there, with or without tiles
under it.

## Bulk reference data

Emergency services, infrastructure, anything public you want on the map before
you start: `tools/osm_places_to_csv.py` builds a Location import CSV from
OpenStreetMap for a bounding box — police, fire, hospitals and jails by
default. Run it, then **Entities → Import** and choose Location. See
[`tools/README.md`](../../tools/README.md).

Two things to know before you do. The importer **does not deduplicate**, so
keep the CSV you imported and use `--skip-ids-from` next time rather than
running it twice. And OpenStreetMap is a volunteer map: a station can be
missing, closed or sited approximately, so treat what lands as a starting set
to correct rather than as a roster.

## Zones: marking off ground

A zone is a NOTAM for your case file — an area, how workable it is, and for how
long. **Zone → Draw** on the Map page, pick polygon, rectangle or circle, and
draw it. Then name it, set the environment, and give it an end time if it has
one.

The colour is the assessment, and it is a traffic light: **green permissive,
yellow semi-permissive, red non-permissive**, a darker and heavier red for
denied, grey for unknown. The legend sits above the map. Location pins use the
same scale from their own environment field, so a green pin inside a red zone
is a contradiction you can see rather than one you have to click twice to
find — and a grey pin means nobody has assessed that place, not that it is
fine. Zones can
overlap, which is the point — a protest inside a district that is separately
non-permissive is two different facts.

**Give it an Event** when it is one. Typing an event name in the zone form
creates the Event record and ties the zone to it, so every report you write
about the protest gathers in one place and *stays there after the zone ends*.
Leave it blank for standing ground that is not an event.

### Keep it current, and say why

The whole value of a tracked zone is the changes. When the protest turns, open
the zone, change the environment, and **fill in why** — "police line moved onto
the bridge". That note goes on the zone's own timeline with the time and your
name, and it is the thing that will make sense of the night when you write it
up next week.

### Expiry does not delete

When the end time passes, the zone goes dashed and faint. It does not
disappear, its Event keeps every report tied to it, and any Location inside it
still lists it — marked as ended. *Show expired* on the map toolbar hides them
if you want a clean picture of right now.

Deleting a zone is admin-only and takes the timeline with it. Letting one
expire is the normal way to finish with a zone.

### What is underneath

Opening a zone lists the Location records inside its boundary. Nothing is
written to those records — it is a live answer, so a zone that ends leaves no
stale marking behind. A Location's own page asks it the other way round, and
lists the zones over it.

## The assistant, if you have a model

Two things, both of which only ever write into the review queue:

- **Ask it to propose links** — the box at the top of Review. Name what you
  want it looked at: "link the Smiths", "connect everyone at the rail yard".
- **The AI Assistant panel** — ask questions about the case file. It can only
  see your records; it has no access to anything else and no ability to write.

It is a reader and a proposer. It is not a source, and nothing it says belongs
in a report without you checking it against the record.

---

## Habits worth having

- **Review the queue before it gets long.** Twenty cards is a working session.
  Two hundred is a chore you will start skimming, and skimming is how a bad
  proposal gets into the file.
- **Merge as you go.** Duplicates breed: every new document names the same
  person a fourth way, and the correlation pass then proposes every pair.
- **Write the note when you make the decision**, not later. You will not
  remember why something was only possible.
- **Check the dashboard once a day.** Urgent reports, forgotten drafts,
  records lapsing, people whose disposition is not *at liberty* — all computed
  from your own records, no model involved.
- **Back up before anything large.** Before a bulk import, before a restore,
  before a big merge session.
