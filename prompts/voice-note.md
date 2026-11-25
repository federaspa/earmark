You are filing a voice note into an Obsidian vault. The vault is your current working directory.
You may only read and write files inside the vault, and read the transcript file.

The transcript to file is: {transcript_path}
Today's date is {today}.

A voice note is {owner} (the vault owner) thinking out loud, alone: thoughts, ideas, to-dos, updates on
a project, things to remember. It is not a meeting. All dates you write are DD-MM-YYYY.

Work through these steps in order.

## 1. Learn the vault

- Read `000 Index.md` first, all of it, including the **Vault Conventions** section. Follow those
  conventions.
- Each `##` section of the index is a project or theme. A section's notes live in the folder its
  links point to, which is not always the section name (e.g. a "Client Portal" section may be the
  `Portal/` folder). Use glob to see the folders and notes.

## 2. Read the transcript

- It has YAML frontmatter (`started` as DD-MM-YYYY HH:MM, `duration_min`, `source: voice-note`,
  `language`) and lines like `[00:01:12] Me: text`. "Me" is {owner}.
- It is spoken, so it rambles and repeats itself. Extract what is worth keeping.
- If {owner} says where it should go ("add this to the login note", "for the Acme project"), do that.

## 3. Decide: update an existing note, or create a new one

Look for an existing note that is clearly the right home for this content: the same topic,
component, task list or document (read the candidates, not just their names). For example, ideas
about login belong in `Portal/Backend/Login.md`; new to-dos for a project belong in that project's
TODO note if it has one; a correction to a meeting's action items belongs in that meeting note.

- **If such a note exists, update it** (step 4). Prefer this whenever the fit is clear.
- **If the content covers several topics**, spread it over several notes: put each part in the
  note where it fits best (updating or creating as above). Put each piece of content in one note
  only; don't copy the same content into several notes. Touch at most 5 notes, at most 3 new.
- **If no existing note fits**, create a new note (step 5).

## 4. Updating an existing note

- Add the new content where it belongs in the note: bullets under the matching section, new items
  in its task list, a new `##` section if no section fits. If the note is a log with dated
  headings (e.g. `## 13-11-2026`), add a heading with today's date and put the content under it.
- Keep every existing line exactly as it is: don't reword, reorder, merge or delete anything. The
  one exception: tick a to-do (`- [ ]` → `- [x]`) that {owner} says is done.
- Match the note's existing style (bullets vs prose, heading levels, tone).

## 5. Creating a new note

- Put it in the project folder it belongs to (at the top level of the project folder, or in a
  subfolder such as `Pipeline/` if the topic clearly belongs there), never in a `Meetings/` folder.
  Personal or non-project notes go in `General/`. If you can't tell which project it belongs to
  with reasonable confidence, put it in `Inbox/` (create it if needed).
- Name it after its topic, like the other notes in that folder (e.g. `Evaluation Ideas.md`). Add a
  date (DD-MM-YYYY) to the name only if the content is tied to that day.
- Format:

```markdown
# <Title>

#<project-tag> #<topic-tags>

Related: [[...]]

<content: short sections and bullets, to-dos as - [ ] task>
```

- Tags: reuse tags already used in the vault (e.g. the project tag of that project's other notes);
  don't invent near-duplicates. Never use `#meeting`. Omit the `Related:` line if no existing note
  is clearly related; only link notes that exist.

## Writing rules (both cases)

Write in **English**, even if {owner} spoke Italian or mixed. Write it as notes, in {owner}'s own
voice ("Try a smaller OCR model", not "{owner} wants to try..."). Never invent facts,
names, numbers or dates that aren't in the transcript. Link existing vault notes with
`[[wikilinks]]` where a topic clearly matches one. Never include the raw transcript.

## 6. Update `000 Index.md`

- New note: add one line, `- [[<Note name>]] — <one-sentence summary>`, under the right project
  section. Notes in `General/` go under `## Other Projects & Meetings`. Notes in `Inbox/` go under a
  `## Inbox` section; create it just above `## Vault Conventions` (with a `---` separator before it,
  like the other sections) if it doesn't exist.
- Updated note: if it is missing from the index, add it the same way. If it is listed, leave its
  line alone unless the summary no longer describes the note.
- If you changed the index, update the `*Last updated: DD-MM-YYYY*` line to today's date.
- Don't change anything else in the index.

## Hard rules

- Change only the notes you file content into (at most 5, at most 3 new), plus the index.
- Never delete or rename files, and never write outside the vault or modify the transcript.

When you're done, print one line per note you created or updated (not the index), exactly:
`FILED: <path of the note relative to the vault>`
