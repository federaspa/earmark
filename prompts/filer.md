You are filing a meeting transcript into an Obsidian vault. The vault is your current working
directory. You may only read and write files inside the vault, and read the transcript file.

The transcript to file is: {transcript_path}
Today's date is {today}.

Work through these steps in order.

## 1. Learn the vault

- Read `000 Index.md` first, all of it, including the **Vault Conventions** section. Follow those
  conventions.
- Each `##` section of the index is a project or theme. A section's notes live in the folder its
  links point to, which is not always the section name (e.g. a "Client Portal" section may be the
  `Portal/` folder). Use glob to see which folder each section's notes live in.

## 2. Read the transcript

- It has YAML frontmatter (`started` as DD-MM-YYYY HH:MM, `duration_min`, `source`, `language`)
  and lines like `[00:12:31] Speaker: text`.
- "Me" is {owner} (the vault owner). "Others" is everyone else on the call, mixed together;
  Teams transcripts carry real speaker names instead.
- The meeting date is the date in `started`.

## 3. Choose the project folder

Decide which project the meeting belongs to using the index, the project overview notes (the note
named after each project folder, e.g. `Portal/Portal.md`), and earlier meeting notes:
participants, client names, products and topics.

- The note goes in `<Project>/Meetings/`. Create that subfolder if it doesn't exist yet.
- Never put a meeting note at the top level of a project folder.
- `General/Meetings/` is only for meetings that are clearly not about project or client work:
  1:1s, PhD/research chats, internal team or HR topics, personal admin.
- If the meeting is about project or client work but the transcript doesn't let you tell with
  reasonable confidence which project (no project, client, product or participant names that match
  the vault), put it in `Inbox/` (create the folder if needed). Don't guess: a wrong project or
  `General/` for project work is worse than `Inbox/`.

## 4. Choose the file name

- If the meeting continues an existing series (for example, `Portal/Meetings/` contains
  `Meeting Acme 07-09-2026.md` and this is another meeting with Acme), name the note to match the
  series: `Meeting Acme DD-MM-YYYY.md`, using the meeting date.
- Otherwise name it `Meeting <Short Topic> DD-MM-YYYY.md` (2–4 words of topic).
- **If a note with exactly that name already exists** (typically a preparation note written before
  the meeting), do not create a second file. Fill in the existing note instead: keep every existing
  line exactly as it is, and append the generated `---` and sections from step 5 below its existing
  content. Do not add a second H1, tags line or link line if they already exist. If the existing
  note has items to discuss (e.g. "Talk about: ..."), reflect in the Discussion section whether
  they were covered, only if the transcript says so.

## 5. Write the note

Write in **English**, even if the meeting was in Italian or mixed. Never invent facts, names,
numbers or dates that aren't in the transcript. Attribute an action item to a person only when the
transcript makes it clear who owns it; otherwise leave it unassigned (`- [ ] task`). Write
"{owner}" for "Me". Refer to people by name or role, never with gendered pronouns (he/she). If the transcript gives no names for "Others", don't make any up — write
"the client", "the team", etc. as the context allows.

Use exactly this format, matching the existing meeting notes (look at a recent note in the
project's `Meetings/` folder as a reference):

```markdown
# Meeting <Series or Topic> — DD-MM-YYYY

#<project-tag> #meeting #<topic-tags>

Previous: [[Meeting <Series> DD-MM-YYYY]]

---

## Summary
3–6 sentences.

## Decisions
- ...

## Action Items
- [ ] Owner — task (due date if mentioned)

## Open Questions
- ...

## Discussion
### <Topic>
Short, only what's worth keeping.
```

Rules for the format:
- Every date is DD-MM-YYYY: in the file name, the H1, the link line and the note text.
- Tags: reuse tags already used in the vault (grep for them, e.g. the project tag used by that
  project's other notes, `#meeting`, series tags like `#acme`). Don't invent near-duplicates
  (`#arabic-onprem` when `#arabic-on-prem` exists). Use lowercase-hyphenated tags.
- Link line: `Previous: [[...]]` for a series continuation. For a new meeting with clearly related
  vault notes, use `Related: [[...]]` instead. Omit the line if neither applies.
- Omit any section that would be empty. Never include the raw transcript or long quotes.
- Link existing vault notes with `[[wikilinks]]` where a topic clearly matches one (e.g.
  [[Architecture]], [[Requirements]]). Only link notes that exist.

## 6. Link the previous note in the series

If you used `Previous: [[X]]`, open note X and add ` · Next: [[<new note name>]]` to the end of its
`Previous:` / link line. If X has no link line, add a line `Next: [[<new note name>]]` right after
its tags line, followed by a blank line. Change nothing else in X. Skip this step if X already
links to the new note.

**If X already has `Next: [[Y]]`** (Y is a later note in the series, e.g. a preparation note for
an upcoming meeting), the new note goes between X and Y in the chain:
- in the new note, write the link line as `Previous: [[X]] · Next: [[Y]]`;
- in X, replace `[[Y]]` in its `Next:` link with `[[<new note name>]]` (don't add a second `Next:`);
- in Y, replace `[[X]]` in its `Previous:` link with `[[<new note name>]]`.
Change nothing else in X or Y. This also applies when you filled in an existing note (step 4) that
already has a `Previous: [[X]]` line.

## 7. Update `000 Index.md`

- Add one line, `- [[<Note name>]] — <one-sentence summary>`, under the right project section, in
  its **Meetings** sub-heading if it has one, keeping meeting entries in chronological order
  (by date, not alphabetically: DD-MM-YYYY names don't sort by date).
- If the note already existed (step 4) and is missing from the index, add it the same way. If it is
  already listed, update its summary only if it was a placeholder.
- Notes in `General/Meetings/` go under `## Other Projects & Meetings`.
- Notes in `Inbox/` go under a `## Inbox` section; create it just above `## Vault Conventions`
  (with a `---` separator before it, like the other sections) if it doesn't exist.
- Update the `*Last updated: DD-MM-YYYY*` line to today's date.
- Don't change anything else in the index.

## Hard rules

- Never delete, rename or rewrite existing notes beyond the specific edits in steps 4, 6 and 7.
- Never write outside the vault, and never modify the transcript.
- Create at most one new note.

When you're done, print one final line, exactly:
`FILED: <path of the meeting note relative to the vault>`
