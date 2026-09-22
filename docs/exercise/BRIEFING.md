# Halvard Industrial — extraction exercise

Eight documents that take an empty instance to an organization report showing a
plot. Everything in them is invented: the county, the newspaper, the company,
the militia, every person, address, phone number and email. Each file says so on
its face.

**The documents are deliberately rich in names, roles, dates and places and
deliberately empty of method.** There is nothing in this set about how anything
would be done, because a case file records who, where and when — and an
extraction exercise needs entities to pull out, not instructions.

---

## What you are meant to find

Nothing in any single document says it. The picture only exists once several are
accepted:

**Halvard Industrial Services** is a real maintenance contractor with a real
county contract and 140 real employees. It is not a front. That is what makes it
worth the exercise — the company is exactly what it says it is, and four of its
staff are something else as well.

The chain, in the order the documents give it up:

1. **Document 1** puts the company, its executives and its county contract in the
   file. Access to a water treatment plant and two public buildings, all
   legitimate.
2. **Document 2** puts the Wolf Creek Constitutional Regiment in the file, with
   Dale Ransom and Curtis Ondaatje. Nothing connects the two.
3. **Document 3** — the about-us page — is the densest source of people. Note
   Trombley's remit: *"site access credentialing and the company's contractor
   badge programme."* One person, no oversight named.
4. **Document 4** is the first contact between the two worlds: company vehicles
   leaving the yard for a weekend, booked as something else, with an outside
   address copied in. Still explicable as a favour.
5. **Document 5** is the money. Three years of payments to a vendor with no
   statement of work, no purchase order and no W-9, every invoice approved by
   the same person. The amounts step up twice — once after the county contract,
   once after the plant badges were issued.
6. **Document 6 is the hinge.** The roster's *day employer* column is what turns
   two organisations into one set of people: Trombley is the regiment's
   quartermaster, Mabey its motor sergeant, and two more Halvard staff are in
   the ranks. The remit-to address on the payables extract and the regiment's
   muster point are the same address.
7. **Document 7** is the plot: a date tied to a public ceremony when both
   buildings are thinly staffed, contractor badges drawn against the company
   account, a night walk-through of the plant with regiment members written onto
   a Halvard crew sheet, vehicles staged, roles assigned, a go/no-go meeting.
8. **Document 8** is the insider account that ties it together and is careful
   about what she does and does not know — which is what a good source statement
   looks like.

### The joins that matter

| Join | Documents |
|---|---|
| Trombley is a Halvard director **and** the regiment's quartermaster | 3 + 6 |
| Mabey runs the fleet **and** is the regiment's motor sergeant | 3 + 6 |
| Wolf Creek Holdings' remit-to address is the regiment's muster point | 5 + 6 |
| The vendor's registered agent is the regiment's commander | 5 + 2 |
| Payment increases track the contract award and the badge issue | 5 + 1 + 7 |
| Badge programme has one administrator and no oversight | 3 + 7 + 8 |
| The walk-through puts regiment members on a Halvard crew sheet | 6 + 7 |

---

## Running it

**Before you start:** Ollama on, with an extraction model pulled. Admin → Ollama
Settings will tell you if it is not — a missing model is the one failure that
otherwise looks exactly like "the documents were boring".

1. **Upload all eight** to the Documents tab in one drop. They are numbered in
   the order above, which is roughly the order a real case would acquire them.
   Give the worker a minute; the two scans take longest.
2. **Work document 3 first** — the about-us page. It gives you the people with
   correct titles, so everything afterwards has records to attach to. Correct the
   entity type where the model guesses wrong; a CV-shaped page routinely gets an
   employer typed as a person.
3. **Then 1, 2 and 6.** By the end of the roster you should have both
   organisations and everyone in them. Watch what happens on the relationship
   cards as you go: names you have already accepted start matching themselves,
   and the ones that do not can be created from the card.
4. **Then 4, 5, 7 and 8** — these are mostly relationships onto records you now
   have.
5. **Turn on the signal pass** (`LINK_SIGNALS_ENABLED=true`) and let it run, or
   run it by hand:
   ```
   docker compose exec -e LINK_SIGNALS_ENABLED=true worker \
     python -c "import link_signals; print(link_signals.run_link_signals(force=True))"
   ```
   The shared Beddoe Ridge address and the shared employer should surface pairs
   you have not linked yourself.
6. **Ask the assistant** to propose links: *"connect everyone at Halvard
   Industrial"*, then *"link the Wolf Creek regiment"*. Its proposals land in the
   review queue with their reasoning.

## The report at the end

Export an **Organization report** on either organisation — Entities → the
record → Export → Organization report.

- **On Halvard Industrial Services** it reads as a company with a county
  contract, an executive team, and four employees who also hold rank in an armed
  group, one of whom controls site access.
- **On the Wolf Creek Constitutional Regiment** it reads as an armed group whose
  quartermaster and motor sergeant run the fleet and the badges at a contractor
  with keys to a water treatment plant.

The second one is the more striking export. Both are the same graph read from
different ends, which is worth seeing.

**One step the documents cannot do for you.** The dossier collects records,
relationships and *reports*. Documents are evidence, not analysis — so write one
short report before you export, linking the entities you have accepted and
stating the assessment in your own words. That is the analyst's job and the
export is built to carry it. Without it the dossier shows a network with no
argument; with it, it shows a case. Give it a Flash or Immediate precedence and
the severity colour carries into the PDF.

---

## What is not in here

No method, no materials, no technique, and no defeat of any real security
measure. The plot is established through access, timing, roles and movement —
the things a case file actually records and the things an extraction pipeline is
supposed to find. If you want a harder test of the model, make the documents
longer and noisier rather than more specific.

The two scanned pages are the real test of your setup: they go through OCR
before the model sees them, so if those two produce noticeably worse proposals
than the six text documents, the problem is the scan quality or Tesseract, not
your model.
