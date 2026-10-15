-- filter.lua
-- Pandoc Lua filter for the report. Single filter that:
--
--   (1) Renders every pandoc Table as a raw-LaTeX longtable with:
--         * auto-sized column widths (weight = 2 * longest_unbreakable_token
--           + sqrt(max_content_length); short cols don't get over-allocated,
--           long-text cols don't starve neighbours), or, for a table inside
--           a div with the class widths-from-dashes, the relative widths of
--           its separator dashes;
--         * p{...} columns aligned as the Markdown marks them (left by
--           default, "---:" right, ":-:" centre), without mid-word
--           hyphenation;
--         * a faint rule between body rows (\LTrowrule in header.tex),
--           drawn once with the page break allowed only after it, and a
--           "Table N (continued)" line above the header that longtable
--           repeats on each continuation page;
--         * single-bold-cell rows rendered as \multicolumn section labels.
--
--   (2) Rewrites every \texttt{...} cell content (and inline Code nodes
--       in body prose, and \href{}{display} link text) to allow line
--       breaks at identifier boundaries: ::, ., /, ",", ":", camelCase
--       humps, before (/[/<, after )/]/>, and within snake_case/kebab-case
--       at _/-. These break points are RANKED by penalty (see the ladder at
--       WORD_BREAK below) so the engine wraps at a "/" or a space in
--       preference to splitting an identifier at "_", and only splits at
--       "_" when the name does not fit whole on the next line. A file name
--       never breaks before its extension and breaks at its other dots only
--       as a last resort, as a line range does at its "-"; a leading "/" or
--       "~/" stays with the name after it, and a decimal number stays whole.
--       Long all-alphabetic runs (>=14 chars) get a last-resort
--       letter-by-letter break at the highest penalty.
--
--   (3) Discourages line breaks inside bracketed units ((..), [..], <..>):
--       LaTeX prefers the break-before-opener we already provide. Same
--       bracket vocabulary as (2); the rules are wired together.
--
-- Loaded via: pandoc --lua-filter=filter.lua

local TOTAL_WIDTH = 0.95   -- fraction of \textwidth available for columns
local MIN_CHARS = 3        -- floor on per-column character weight
local SOFT_SCALE = "sqrt"  -- "max", "sqrt": dampen long-text columns
local MAX_FRACTION = 0.50  -- cap any single column to this share

-- ----- helpers: stringify cell contents for width estimation -----

local function inline_text(inl)
  local t = inl.t or inl.tag
  if t == "Str" then return inl.text or "" end
  if t == "Space" or t == "SoftBreak" or t == "LineBreak" then return " " end
  if t == "Code" then return inl.text or "" end
  if t == "RawInline" then
    -- Dash glyph macros (see Str below) are 1-glyph wide; counting their
    -- macro-name characters would over-allocate range columns like "12-16".
    local txt = inl.text or ""
    txt = txt:gsub("\\textendash{}\\allowbreak{}", "-")
             :gsub("\\textemdash{}\\allowbreak{}", "--")
             :gsub("\\textendash{}", "-"):gsub("\\textemdash{}", "--")
    return txt
  end
  if inl.content then
    local s = ""
    for _, child in ipairs(inl.content) do s = s .. inline_text(child) end
    return s
  end
  return ""
end

local function blocks_text(blocks)
  if not blocks then return "" end
  local out = ""
  for _, blk in ipairs(blocks) do
    local t = blk.t or blk.tag
    if t == "Plain" or t == "Para" then
      for _, inl in ipairs(blk.content or {}) do
        out = out .. inline_text(inl)
      end
    elseif blk.content then
      for _, child in ipairs(blk.content) do
        out = out .. inline_text(child)
      end
    end
    out = out .. " "
  end
  return out
end

-- ----- helpers: insert break opportunities inside \texttt{} content -----
-- Long identifiers won't break to fit narrow columns by default, so we
-- insert break opportunities. The PENALTY of each opportunity encodes a
-- readability hierarchy. This matters because the cells are \raggedright:
-- with \rightskip=0pt plus 1fil every line has badness 0, so a break's
-- penalty is the ONLY thing that ranks it above another. If every
-- separator were \allowbreak (penalty 0), as they once were, TeX sees
-- a flat tie and just fills each line greedily, landing the break on
-- whatever separator happens to sit nearest the line edge (typically an
-- "_" mid-identifier). The ladder, lowest = most preferred:
--
--   penalty 0   (\allowbreak)  spaces*, "/", "::", ".", ",", and before "<"
--                              / after ")" "]" ">": boundaries BETWEEN
--                              distinct named units, the cleanest wraps.
--   penalty 250 (WORD_BREAK)   "_": a word separator WITHIN one identifier.
--                              Above the structural tier so a nearby space
--                              or "/" wins; an identifier is split at "_"
--                              only when it does not fit whole on the next
--                              line.
--   penalty 300 (OPEN_BREAK)   before "(" and "[": a function name stays with
--                              its argument list unless keeping them
--                              together costs more than a break at "_".
--   penalty 500 (HYPHEN_BREAK) after "-" (except between two digits), a
--                              camelCase hump and a single ":". A line that
--                              ends in "-" inside a name reads as
--                              hyphenation, a hump has no visible separator,
--                              and a ":" joins a key to its value
--                              (`key: value`) or a file to its line numbers
--                              (`file.cpp:125`), so such a name is split
--                              there only when the lower-penalty breaks give
--                              a worse paragraph.
--   penalty 5000 (LATE_BREAK)  after a "." inside a file name that ends in a
--                              short extension, and after a "-" between two
--                              digits (a line range such as 125-131), so that
--                              `pf_localization.launch.py` splits at a dot and
--                              a line range at its "-" only when no other
--                              break gives lines that fit (without the break,
--                              a narrow column or a justified line can get an
--                              overfull box or a very loose line).
--   penalty 5000 (bracket sp)  a space inside a PROSE (..) (see
--                              tie_bracket_spaces; a no-op for code, which
--                              lives inside \texttt{}).
--   penalty 9999 (LONG_RUN)    last-resort letter-by-letter split (below).
-- (*real interword spaces are glue, not penalty nodes, but rank with 0.)
-- The braces end the penalty's number, so a space that follows in the
-- source ("a - b") stays a space; a bare "\penalty 250 " would take that
-- space as the end of the number and print "a -b". The colon's penalty
-- needs them too: without them the digits of "file.cpp:125" would become
-- part of the number.
local WORD_BREAK = "{\\penalty 250}"
local OPEN_BREAK = "{\\penalty 300}"
local HYPHEN_BREAK = "{\\penalty 500}"
local LATE_BREAK = "{\\penalty 5000}"

-- Breaks the ladder leaves out:
--   * after a "." that a digit follows, so that a decimal number such as
--     0.025 stays whole;
--   * after a "." that one to five letters and then the end of the name
--     follow, the extension of a file name;
--   * after a "/" that starts a name or follows "~", so that a line does not
--     end in a lone "/" or "~/".
-- The other dots of a file name that ends in a short extension (see
-- ends_in_extension) break at LATE_BREAK only.

-- Characters of a name for ends_in_extension: letters, digits, "." and "-",
-- and the escaped underscore "\_".
local function name_bounds(body, i)
  local a = i
  while a > 1 do
    if body:sub(a-1, a-1):match("[%w%.%-]") and body:sub(a-2, a-1) ~= "\\_" then
      a = a - 1
    elseif body:sub(a-2, a-1) == "\\_" then
      a = a - 2
    else
      break
    end
  end
  local b = i
  while b < #body do
    if body:sub(b+1, b+2) == "\\_" then
      b = b + 2
    elseif body:sub(b+1, b+1):match("[%w%.%-]") then
      b = b + 1
    else
      break
    end
  end
  return a, b
end

-- True when the name around position i ends in a dot and one to five
-- letters, as `romb_cola_bridge.launch.py` and `ekf.cpp:118` do.
local function ends_in_extension(body, i)
  local a, b = name_bounds(body, i)
  local name = body:sub(a, b)
  return name:match("%.%a%a?%a?%a?%a?$") ~= nil
end

-- True when a "/" at position i starts a name: it is the first character,
-- or a space, an opening bracket, ",", "=", ":" or "~" precedes it.
local function slash_starts_name(body, i)
  if i == 1 then return true end
  local before = body:sub(1, i-1)
  if before:match("[ %(%[,=:~]$") then return true end
  if before:match("\\textasciitilde{}$") or before:match("\\textasciitilde$") then return true end
  return false
end

-- Last-resort: insert a high-penalty break opportunity between letters
-- in any alphabetic run of >=14 chars with no internal separator.
-- Penalty 9999 (one below the 10000 "forbidden" sentinel) so the engine
-- prefers ANY natural break point (slash, dot, camelCase, ::, _) even
-- when that leaves the broken line short and sparse. Letter breaks fire
-- only when the column is genuinely narrower than any natural-break
-- alternative.
--
-- Earlier versions wrapped the \penalty with `\hskip 0pt plus 0pt` on
-- both sides, intending a zero-width "break-glue". That was a bug:
-- TeX's line-breaker treats every glue node as a candidate break with
-- the GLUE's penalty (which is 0 here), so the flanking \hskips created
-- p=0 candidates AT THE LETTER POSITIONS. Demerits there came out to
-- ~100, tying with a slash-break's ~100; fitness-class adjacency then
-- pushed TeX toward the later (= longer first line) candidate, so long
-- identifiers split mid-letter even when a `/` break was available with
-- the same column space.
--
-- A bare `\penalty 9999` is itself a feasible breakpoint: TeX scans
-- penalty nodes directly without needing a flanking glue node. Demerits
-- for breaking at this penalty are 9999^2 + 100 ~= 1e8, vs ~100 for a
-- slash break (\allowbreak = \penalty 0). The slash now dominates.
local LONG_RUN_BREAK = "\\penalty 9999 "

local function split_long_run(run)
  if #run < 14 then return run end
  local parts = {}
  for i = 1, #run do
    parts[#parts+1] = run:sub(i, i)
    if i < #run then parts[#parts+1] = LONG_RUN_BREAK end
  end
  return table.concat(parts)
end

local function tt_insert_breaks(body)
  -- body is the text inside a \texttt{...} where braces may appear via
  -- \textless{}/\textgreater{}. Scan character by character so we don't
  -- need to balance braces in a regex.
  local out = {}
  local i = 1
  local n = #body
  local prev_is_lower = false
  local current_run = {}        -- pending alphabetic run, flushed at non-letter
  local function flush_run()
    if #current_run > 0 then
      table.insert(out, split_long_run(table.concat(current_run)))
      current_run = {}
    end
  end
  while i <= n do
    local c = body:sub(i, i)
    -- detect multi-char escapes that are atomic markers
    if c == "\\" and body:sub(i, i+1) == "\\_" then
      -- Underscore: within-identifier word separator (snake_case). Break
      -- AFTER it, at WORD_BREAK so a "/" or space is preferred and the
      -- identifier stays whole when it fits on the next line.
      flush_run()
      table.insert(out, "\\_" .. WORD_BREAK)
      i = i + 2
      prev_is_lower = false
    elseif body:sub(i, i+8) == "\\textless" then
      -- Break BEFORE < (so identifier< wraps as identifier / <Template>)
      flush_run()
      local j = i + 9
      if body:sub(j, j+1) == "{}" then j = j + 2 end
      table.insert(out, "\\allowbreak{}" .. body:sub(i, j-1))
      i = j
      prev_is_lower = false
    elseif body:sub(i, i+11) == "\\textgreater" then
      -- Break AFTER > (close-template, then continue)
      flush_run()
      local j = i + 12
      if body:sub(j, j+1) == "{}" then j = j + 2 end
      table.insert(out, body:sub(i, j-1) .. "\\allowbreak{}")
      i = j
      prev_is_lower = false
    elseif c == "\\" then
      -- Generic LaTeX macro \name (followed by optional balanced {arg}).
      -- The two special-cased macros \textless / \textgreater already
      -- matched above; this catches \textasciitilde, \textbackslash,
      -- and any other \word emitted by latex_escape or by pandoc. Treat
      -- the whole token as atomic so the long-run letter-walker doesn't
      -- shred the macro name.
      flush_run()
      local j = i + 1
      while j <= n and body:sub(j, j):match("[A-Za-z]") do j = j + 1 end
      if body:sub(j, j) == "{" then
        local depth = 1
        j = j + 1
        while j <= n and depth > 0 do
          local cc = body:sub(j, j)
          if cc == "{" then depth = depth + 1
          elseif cc == "}" then depth = depth - 1
          end
          j = j + 1
        end
      end
      table.insert(out, body:sub(i, j-1))
      i = j
      prev_is_lower = false
    elseif c == ":" and body:sub(i+1, i+1) == ":" then
      flush_run()
      table.insert(out, "::\\allowbreak{}")
      i = i + 2
      prev_is_lower = false
    elseif c == "." or c == "/" then
      -- Structural separators (path "/", member/segment "."): break AFTER,
      -- at penalty 0: they sit between distinct named units, so they are
      -- the preferred place to wrap (above the WORD_BREAK "_" tier).
      flush_run()
      -- Exception: don't break after `.` when the trailing run looks like a
      -- file extension (1-5 alpha chars to end-of-string or non-alphanumeric).
      -- Otherwise we get ugly splits like `clouddistancecalculator.` / `cpp`.
      -- The earlier `/` break in a path will be preferred by LaTeX instead.
      local suppress = false
      -- Do not break after a dot that a digit follows, so that a decimal
      -- number such as 0.025 or 16.3 stays whole.
      if c == "." and body:sub(i+1, i+1):match("%d") then
        suppress = true
      end
      if c == "." then
        local j = i + 1
        while j <= #body and body:sub(j, j):match("[A-Za-z]") do j = j + 1 end
        local run = j - i - 1
        if run >= 1 and run <= 5 then
          local nxt = body:sub(j, j)
          if j > #body or not nxt:match("[A-Za-z0-9_]") then
            suppress = true
          end
        end
      end
      -- A file name with an extension keeps its dots unless nothing else
      -- fits: a break after the first dot of `pf_localization.launch.py`
      -- reads as two names, so it costs LATE_BREAK.
      local file_dot = c == "." and not suppress and ends_in_extension(body, i)
      -- A "/" that starts a name stays with it: `/ekf_node/pose`, `~/romb_fix`.
      if c == "/" and slash_starts_name(body, i) then
        suppress = true
      end
      if suppress then
        table.insert(out, c)
      elseif file_dot then
        table.insert(out, c .. LATE_BREAK)
      else
        table.insert(out, c .. "\\allowbreak{}")
      end
      i = i + 1
      prev_is_lower = false
    elseif c == "," then
      -- Break after a comma at penalty 0, like "/", because a comma separates
      -- list items such as the line ranges in file.cpp:125-131,167-172.
      flush_run()
      table.insert(out, ",\\allowbreak{}")
      i = i + 1
      prev_is_lower = false
    elseif c == ":" then
      -- Break after a single colon (a "::" is handled above) at penalty 500;
      -- the space in `key: value` then follows a penalty and is not a legal
      -- breakpoint, so a key and its value, or a path and its line numbers,
      -- split at the colon only when the other breaks cost more demerits.
      flush_run()
      table.insert(out, ":{\\penalty 500}")
      i = i + 1
      prev_is_lower = false
    elseif c == "-" then
      -- Hyphen: within-identifier word separator (kebab-case, CLI flags,
      -- line ranges). Break AFTER, at HYPHEN_BREAK: a line that ends in "-"
      -- inside a name reads as a hyphen TeX inserted, so "/", spaces and
      -- "_" are preferred (e.g. keep "--pdf-engine" together).
      flush_run()
      -- A "-" between two digits joins the ends of a line range; a line that
      -- ends in "125-" reads as a broken number, so there it costs LATE_BREAK.
      if body:sub(i-1, i-1):match("%d") and body:sub(i+1, i+1):match("%d") then
        table.insert(out, c .. LATE_BREAK)
      else
        table.insert(out, c .. HYPHEN_BREAK)
      end
      i = i + 1
      prev_is_lower = false
    elseif c == "(" or c == "[" then
      -- Open bracket: break BEFORE (bracketed content stays as a unit), at
      -- OPEN_BREAK so that a function name stays with its argument list.
      flush_run()
      table.insert(out, OPEN_BREAK .. c)
      i = i + 1
      prev_is_lower = false
    elseif c == ")" or c == "]" then
      -- Close bracket: break AFTER.
      flush_run()
      table.insert(out, c .. "\\allowbreak{}")
      i = i + 1
      prev_is_lower = false
    else
      local is_lower = c:match("[a-z]") ~= nil
      local is_upper = c:match("[A-Z]") ~= nil
      if is_upper and prev_is_lower then
        -- camelCase break: end the previous run, start a new one with this
        -- char. At HYPHEN_BREAK, far above \allowbreak{} (which is \penalty
        -- 0), so separator-based break opportunities (after :_./() etc.) are
        -- strongly preferred. Keeps `.maxCoeff()` from being split as
        -- `.max` | `Coeff()` when a break before the whole token exists.
        flush_run()
        table.insert(out, HYPHEN_BREAK)
        table.insert(current_run, c)
      elseif is_lower or is_upper then
        table.insert(current_run, c)
      else
        flush_run()
        table.insert(out, c)
      end
      prev_is_lower = is_lower
      i = i + 1
    end
  end
  flush_run()
  return table.concat(out)
end

-- Spaces inside any bracketed unit (..), [..], <..> should be reluctant
-- to break. The general rule: if a bracketed aside fits within a line,
-- it's more readable to break BEFORE the opener than to break mid-unit.
-- We already insert \allowbreak{} before each opener and after each
-- closer; here we add a high-penalty break opportunity for interior
-- spaces, so LaTeX falls back to mid-unit only when no other choice.
local function tie_bracket_spaces(s)
  -- Walk s, but treat any \macroname{...} as a single atomic unit so we
  -- don't mangle macro arguments that may contain spaces or \penalty
  -- directives placed by tt_insert_breaks. Only literal text outside
  -- macros participates in bracket-depth tracking and space-tying.
  local out = {}
  local depth = 0
  local i = 1
  local n = #s
  while i <= n do
    -- LaTeX-escaped angle brackets: \textless{} / \textgreater{}
    if s:sub(i, i+10) == "\\textless{}" then
      depth = depth + 1
      table.insert(out, "\\textless{}")
      i = i + 11
    elseif s:sub(i, i+13) == "\\textgreater{}" then
      if depth > 0 then depth = depth - 1 end
      table.insert(out, "\\textgreater{}")
      i = i + 14
    elseif s:sub(i, i) == "\\" then
      -- A LaTeX macro \name (followed by optional balanced {arg}).
      -- Emit verbatim; do not let bracket/space rewriting reach into it.
      local j = i + 1
      while j <= n and s:sub(j, j):match("[A-Za-z]") do j = j + 1 end
      -- consume number argument if present (e.g. "\penalty 300 ")
      while j <= n and s:sub(j, j):match("[ %d]") do j = j + 1 end
      -- consume one balanced {...} if present
      if s:sub(j, j) == "{" then
        local d = 1
        j = j + 1
        while j <= n and d > 0 do
          local cc = s:sub(j, j)
          if cc == "{" then d = d + 1
          elseif cc == "}" then d = d - 1
          end
          j = j + 1
        end
      end
      table.insert(out, s:sub(i, j-1))
      i = j
    else
      local c = s:sub(i, i)
      if c == "(" or c == "[" then
        depth = depth + 1
        table.insert(out, c)
      elseif c == ")" or c == "]" then
        if depth > 0 then depth = depth - 1 end
        table.insert(out, c)
      elseif c == " " and depth > 0 then
        -- Breakable but high-cost; LaTeX prefers the break-before-opener.
        table.insert(out, " \\penalty 5000 ")
      else
        table.insert(out, c)
      end
      i = i + 1
    end
  end
  return table.concat(out)
end

local function add_breaks_to_latex(s)
  -- Walk s, find \texttt{...} regions (balanced single-level braces),
  -- and apply tt_insert_breaks to their bodies. Also handle \href{u}{t}.
  local result = {}
  local i = 1
  local n = #s
  while i <= n do
    -- check for \texttt{
    if s:sub(i, i+6) == "\\texttt" then
      local j = i + 7
      if s:sub(j, j) == "{" then
        -- find matching brace
        local depth = 1
        local k = j + 1
        while k <= n and depth > 0 do
          local c = s:sub(k, k)
          if c == "{" then depth = depth + 1
          elseif c == "}" then depth = depth - 1
          end
          k = k + 1
        end
        if depth == 0 then
          local body = s:sub(j+1, k-2)
          -- Idempotency guard. pandoc visits an inline Code node inside a
          -- table cell (handled by Code() -> a \texttt{} with breaks already
          -- inserted) BEFORE the Table filter renders the cell and re-runs
          -- this pass over the same \texttt{}. Without the guard the breaks
          -- get inserted twice (doubled \penalty/\allowbreak nodes). If the
          -- body already carries break markers, pass it through untouched;
          -- otherwise insert them. This is also order-independent: whichever
          -- pass reaches a still-unbroken body does the work.
          if body:find("\\penalty", 1, true) or body:find("\\allowbreak", 1, true) then
            table.insert(result, "\\texttt{" .. body .. "}")
          else
            table.insert(result, "\\texttt{" .. tt_insert_breaks(body) .. "}")
          end
          i = k
        else
          table.insert(result, c)
          i = i + 1
        end
      else
        table.insert(result, s:sub(i, i+6))
        i = i + 7
      end
    else
      table.insert(result, s:sub(i, i))
      i = i + 1
    end
  end
  return table.concat(result)
end

-- ----- helpers: render cell contents to a LaTeX string -----

local function blocks_to_latex(blocks)
  -- Use pandoc.write to get LaTeX for the cell body. We then unwrap any
  -- leading/trailing whitespace and surrounding paragraph wrappers.
  local doc = pandoc.Pandoc({pandoc.Plain(pandoc.utils.blocks_to_inlines(blocks))})
  local s = pandoc.write(doc, "latex")
  -- Strip trailing newline(s)
  s = s:gsub("%s+$", "")
  -- Insert break opportunities inside \texttt{} content
  s = add_breaks_to_latex(s)
  -- Discourage line breaks inside bracketed units: LaTeX should prefer
  -- to break BEFORE the opener over splitting the unit in two.
  s = tie_bracket_spaces(s)
  return s
end

-- ----- helpers: detect a row whose only non-empty cell is a bold inline -----

local function row_section_label(cells)
  local non_empty = {}
  for _, cell in ipairs(cells) do
    local txt = blocks_text(cell.contents or {})
    if txt:match("%S") then table.insert(non_empty, {cell = cell, text = txt}) end
  end
  if #non_empty ~= 1 then return nil end
  local cell = non_empty[1].cell
  -- The cell must contain a single Para or Plain whose only inline is a Strong.
  local blks = cell.contents
  if not blks or #blks ~= 1 then return nil end
  local blk = blks[1]
  if (blk.t or blk.tag) ~= "Plain" and (blk.t or blk.tag) ~= "Para" then return nil end
  local inls = blk.content or {}
  -- Skip leading/trailing whitespace
  local first, last = 1, #inls
  while first <= last and (inls[first].t == "Space" or inls[first].tag == "Space") do first = first + 1 end
  while last >= first and (inls[last].t == "Space" or inls[last].tag == "Space") do last = last - 1 end
  if first ~= last then return nil end
  local only = inls[first]
  if (only.t or only.tag) ~= "Strong" then return nil end
  -- Extract the bold content as LaTeX
  local s = pandoc.write(pandoc.Pandoc({pandoc.Plain(only.content)}), "latex")
  s = s:gsub("\n+$", "")
  -- pandoc.write wraps Strong in \textbf{...}; strip the outer \textbf
  local inner = s:match("^\\textbf{(.+)}$")
  return inner or s
end

-- ----- main: render a Table to raw LaTeX longtable -----

-- Find the longest contiguous alphanumeric run in s, treating any of these
-- as separators: space, _, ., -, /, :, <, >, (, ), [, ], {, }, ,, ;, =, *.
-- This is what actually constrains a column's minimum width: short content
-- in a column where one cell happens to have a long unbreakable identifier
-- still needs enough room to fit that identifier without overflow.
local function longest_unbreakable(s)
  local maxlen = 0
  local cur = 0
  for ch in s:gmatch(".") do
    if ch:match("[%w]") then
      cur = cur + 1
      if cur > maxlen then maxlen = cur end
    else
      cur = 0
    end
  end
  return maxlen
end

-- Shrink the column widths for the glue between the columns (see the
-- comment at the end of compute_widths).
local function shrink_for_glue(widths, n_cols)
  local COLSEP_FRAC = 0.0125
  local glue = (n_cols - 1) * COLSEP_FRAC
  if glue > 0 and glue < TOTAL_WIDTH then
    local shrink = (TOTAL_WIDTH - glue) / TOTAL_WIDTH
    for i = 1, n_cols do widths[i] = widths[i] * shrink end
  end
  return widths
end

local function compute_widths(header_cells, body_rows, n_cols)
  -- Two weights per column:
  --   * unbreak_max: longest unbreakable token. Floors the column width;
  --     short content with one long identifier still needs room.
  --   * content_max: longest cell length. Used to break ties / distribute
  --     the leftover space proportionally to actual content volume.
  local unbreak_max = {}
  local content_max = {}
  for i = 1, n_cols do
    unbreak_max[i] = MIN_CHARS
    content_max[i] = MIN_CHARS
  end
  local function consider(cells)
    for i, cell in ipairs(cells) do
      if i > n_cols then break end
      local t = blocks_text(cell.contents or {})
      local u = longest_unbreakable(t)
      local n = #t
      if u > unbreak_max[i] then unbreak_max[i] = u end
      if n > content_max[i] then content_max[i] = n end
    end
  end
  consider(header_cells)
  for _, row in ipairs(body_rows) do
    if not row_section_label(row) then consider(row) end
  end
  -- Combine: each column's weight is its longest-unbreakable run weighted
  -- heavily (it's the hard constraint), plus a sqrt-of-content term that
  -- gives prose-heavy columns a bit of extra breathing room without
  -- letting them dominate.
  local maxlen = {}
  for i = 1, n_cols do
    maxlen[i] = unbreak_max[i] * 2.0 + math.sqrt(content_max[i])
  end
  -- (kept for compatibility with the cap pass below)
  local scaled = {}
  for i, v in ipairs(maxlen) do scaled[i] = v end
  local sum = 0
  for _, v in ipairs(scaled) do sum = sum + v end
  local widths = {}
  for i = 1, n_cols do
    widths[i] = (scaled[i] / sum) * TOTAL_WIDTH
  end
  -- Cap any single column; redistribute overflow to others proportionally.
  local total_overflow = 0
  for i = 1, n_cols do
    if widths[i] > MAX_FRACTION then
      total_overflow = total_overflow + (widths[i] - MAX_FRACTION)
      widths[i] = MAX_FRACTION
    end
  end
  if total_overflow > 0 then
    local under = {}
    local under_sum = 0
    for i, w in ipairs(widths) do
      if w < MAX_FRACTION then under[i] = w; under_sum = under_sum + w end
    end
    if under_sum > 0 then
      for i, w in pairs(under) do
        widths[i] = w + total_overflow * (w / under_sum)
      end
    end
  end
  -- Inter-column glue budget. The p{...} widths above are normalized to sum
  -- to TOTAL_WIDTH of \textwidth, but even with @{} killing the OUTER padding
  -- each pair of adjacent columns still contributes 2*\tabcolsep (= 6pt at
  -- header.tex's 3pt) of glue, so an n-column table was typeset at
  -- TOTAL_WIDTH*\textwidth + (n-1)*6pt: a deterministic overfull once n >= 6
  -- (7 columns: 11.79pt too wide). Rescale the column widths by the glue
  -- share, expressed as a fraction of \textwidth (6pt / ~484pt text width at
  -- header.tex's 0.9in letter margins; 0.0125 is marginally conservative so
  -- narrower text widths still fit).
  return shrink_for_glue(widths, n_cols)
end

-- ----- column widths from the separator dashes -----
-- A table inside a div with the class widths-from-dashes takes its relative
-- column widths from the dashes of its separator row instead of from its
-- content: around a table with the separator |--|----|------| the div
--   ::: {.widths-from-dashes}
--   ...
--   :::
-- gives the columns 1/6, 1/3 and 1/2 of the table width. Pandoc reads these
-- widths (ColWidth in the colspecs) only when a line of the table is longer
-- than --columns, 72 characters by default; a table without them keeps the
-- widths from its content. pandoc visits a table before the div around it,
-- so a first filter pass marks the tables (see the end of this file) and
-- the Table function reads the mark.
local WIDTHS_CLASS = "widths-from-dashes"

local function mark_widths_from_dashes(div)
  if not div.classes:includes(WIDTHS_CLASS) then return nil end
  return div:walk({
    Table = function(t)
      if not t.classes:includes(WIDTHS_CLASS) then t.classes:insert(WIDTHS_CLASS) end
      return t
    end,
  })
end

-- The colspec widths scaled to TOTAL_WIDTH, then shrunk for the glue between
-- the columns as compute_widths does; nil when a column has no width.
local function widths_from_colspecs(colspecs, n_cols)
  local widths, sum = {}, 0
  for i = 1, n_cols do
    local w = colspecs[i] and colspecs[i][2]
    if type(w) ~= "number" or w <= 0 then return nil end
    widths[i] = w
    sum = sum + w
  end
  for i = 1, n_cols do widths[i] = widths[i] / sum * TOTAL_WIDTH end
  return shrink_for_glue(widths, n_cols)
end

local function rows_from_body(body)
  -- pandoc 3.x: TableBody = {attr, RowHeadColumns, head=[Row], body=[Row]}
  local out = {}
  if body.body then for _, r in ipairs(body.body) do out[#out+1] = r end end
  -- Older shapes: body is an array of rows directly
  if (#out == 0) and body.rows then
    for _, r in ipairs(body.rows) do out[#out+1] = r end
  end
  return out
end

local function header_cells(head)
  if head.rows and head.rows[1] then return head.rows[1].cells end
  if head.content and head.content[1] then return head.content[1].cells end
  return {}
end

local function body_rows_all(bodies)
  local out = {}
  for _, body in ipairs(bodies) do
    for _, row in ipairs(rows_from_body(body)) do
      out[#out+1] = row.cells
    end
  end
  return out
end

function Table(t)
  if not FORMAT:match("latex") then return nil end
  local hcells = header_cells(t.head)
  local n_cols = #hcells
  if n_cols == 0 then return nil end
  local brows = body_rows_all(t.bodies)
  local widths = t.classes:includes(WIDTHS_CLASS)
                 and widths_from_colspecs(t.colspecs, n_cols)
                 or compute_widths(hcells, brows, n_cols)

  -- caption: prefer the long caption Inline list
  local caption_tex = ""
  if t.caption and t.caption.long and #t.caption.long > 0 then
    local doc = pandoc.Pandoc(t.caption.long)
    caption_tex = pandoc.write(doc, "latex"):gsub("\n+$", "")
  end

  local col_spec = {}
  for i = 1, n_cols do
    -- Alignment from the Markdown markers: "---:" right (numeric columns, so
    -- that their digits line up), ":-:" centre, anything else left.
    local a = t.colspecs and t.colspecs[i] and t.colspecs[i][1]
    local align = "\\raggedright"
    if a == "AlignRight" then align = "\\raggedleft"
    elseif a == "AlignCenter" then align = "\\centering" end
    -- Ragged alignment + \sloppy + finite emergencystretch: the line-breaker
    -- is told both that ragged lines are fine AND that it has slack to
    -- avoid overshooting. Without \sloppy + emergencystretch, TeX can
    -- still prefer a high-penalty mid-identifier break over a short, well-
    -- behaved /-break, because the badness model under just \raggedright
    -- doesn't fully neutralise short-line cost in <texttt> cells.
    col_spec[i] = string.format(
      ">{%s\\sloppy\\emergencystretch=3em\\arraybackslash}p{%.4f\\textwidth}",
      align, widths[i])
  end
  local spec = "@{}" .. table.concat(col_spec, " ") .. "@{}"

  local out = {}
  table.insert(out, "\\begin{small}")
  table.insert(out, "\\begin{longtable}{" .. spec .. "}")
  -- Always emit \caption so longtable gets a "Table N." prefix; if the
  -- markdown had no caption text, the prefix stands alone (clean look
  -- with labelsep=period set in header.tex).
  table.insert(out, "\\caption{" .. caption_tex .. "}\\\\")
  table.insert(out, "\\toprule")
  -- header row. If the cell content already typesets as Strong, don't wrap
  -- in another \textbf{}; just use it as-is.
  local hparts = {}
  for _, c in ipairs(hcells) do
    local rendered = blocks_to_latex(c.contents or {})
    if rendered:match("^\\textbf{.+}$") then
      hparts[#hparts+1] = rendered
    else
      hparts[#hparts+1] = "\\textbf{" .. rendered .. "}"
    end
  end
  table.insert(out, table.concat(hparts, " & ") .. " \\\\")
  table.insert(out, "\\midrule")
  table.insert(out, "\\endfirsthead")
  -- The header repeated on a continuation page opens with the table's number,
  -- so that a reader who starts on that page knows which table continues.
  table.insert(out, "\\multicolumn{" .. n_cols .. "}{@{}l@{}}{\\textbf{Table~\\thetable} (continued)} \\\\")
  table.insert(out, "\\toprule")
  table.insert(out, table.concat(hparts, " & ") .. " \\\\")
  table.insert(out, "\\midrule")
  table.insert(out, "\\endhead")

  local body_row_count = 0
  for _, cells in ipairs(brows) do
    local label = row_section_label(cells)
    if label then
      table.insert(out, "\\multicolumn{" .. n_cols .. "}{@{}l@{}}{\\textit{\\textbf{"
                   .. label .. "}}} \\\\")
      table.insert(out, "\\addlinespace")
      body_row_count = 0
    else
      -- \LTrowrule, not \hline: see header.tex.
      if body_row_count > 0 then table.insert(out, "\\LTrowrule") end
      local parts = {}
      for i = 1, n_cols do
        local cell = cells[i]
        if cell then
          parts[i] = blocks_to_latex(cell.contents or {})
        else
          parts[i] = ""
        end
      end
      table.insert(out, table.concat(parts, " & ") .. " \\\\")
      body_row_count = body_row_count + 1
    end
  end

  table.insert(out, "\\bottomrule")
  table.insert(out, "\\end{longtable}")
  table.insert(out, "\\end{small}")
  return pandoc.RawBlock("latex", table.concat(out, "\n"))
end

-- ----- unicode dashes: ligature-independent glyph macros -----------------
-- The LaTeX writer's `smart` extension (a WRITER default, active even when
-- the reader parses with -smart) turns U+2013/U+2014 in body text into the
-- TeX ligature sequences "--"/"---". The Makefile disables font ligatures
-- (RawFeature=-tlig on main/sans), so those sequences rendered as literal
-- hyphen runs: every en/em dash in the document silently degraded. Emit
-- \textendash{}/\textemdash{} instead: real dash glyphs regardless of
-- ligature settings, in prose AND in table cells (this filter runs on cell
-- content before the Table renderer stringifies it). A trailing \allowbreak
-- restores TeX's native break-after-dash opportunity, which the bare macro
-- would otherwise remove (e.g. inside "pp. 629--635"-style ranges).
-- Metadata (title/subtitle) is not affected: pandoc renders template
-- variables through a path that keeps the unicode characters, which the
-- fonts cover natively.
local EN_DASH = "\226\128\147"   -- U+2013 as UTF-8 bytes
local EM_DASH = "\226\128\148"   -- U+2014
function Str(el)
  if not FORMAT:match("latex") then return nil end
  local t = el.text
  if not (t:find(EN_DASH, 1, true) or t:find(EM_DASH, 1, true)) then return nil end
  local out = {}
  local buf = {}
  local i = 1
  local n = #t
  local function flush()
    if #buf > 0 then out[#out+1] = pandoc.Str(table.concat(buf)); buf = {} end
  end
  while i <= n do
    if t:sub(i, i + 2) == EN_DASH then
      flush(); out[#out+1] = pandoc.RawInline("latex", "\\textendash{}\\allowbreak{}")
      i = i + 3
    elseif t:sub(i, i + 2) == EM_DASH then
      flush(); out[#out+1] = pandoc.RawInline("latex", "\\textemdash{}\\allowbreak{}")
      i = i + 3
    else
      buf[#buf+1] = t:sub(i, i); i = i + 1
    end
  end
  flush()
  return out
end

-- ----- inline Code, RawBlock, RawInline: apply the same break rules ----

-- LaTeX-escape raw user code (from markdown backticks). Mirrors pandoc's
-- own escaping but explicit so we can feed the result through
-- tt_insert_breaks before re-wrapping in \texttt{}.
local function latex_escape(s)
  s = s:gsub("\\", "\\textbackslash{}")
  s = s:gsub("([&%%#%${}_])", "\\%1")
  -- "^" escaped as "\^" would be the circumflex accent on the next
  -- character; \textasciicircum prints the caret itself.
  s = s:gsub("%^", "\\textasciicircum{}")
  s = s:gsub("\\textbackslash{}", "\\textbackslash{}")  -- restore
  s = s:gsub("<", "\\textless{}")
  s = s:gsub(">", "\\textgreater{}")
  s = s:gsub("~", "\\textasciitilde{}")
  return s
end

function Code(elem)
  if not FORMAT:match("latex") then return nil end
  local escaped = latex_escape(elem.text)
  local broken = tt_insert_breaks(escaped)
  return pandoc.RawInline("latex", "\\texttt{" .. broken .. "}")
end

function RawBlock(elem)
  if elem.format ~= "latex" and elem.format ~= "tex" then return nil end
  elem.text = add_breaks_to_latex(elem.text)
  return elem
end

function RawInline(elem)
  if elem.format ~= "latex" and elem.format ~= "tex" then return nil end
  elem.text = add_breaks_to_latex(elem.text)
  return elem
end

-- ---------------------------------------------------------------------------
-- Vector-figure promotion. Chart scripts write each plot twice with the same
-- basename: a raster .png (what the Markdown references, so plain-Markdown
-- preview keeps working) and a vector .pdf sibling. For the LaTeX build only,
-- an image whose .png source has a .pdf sibling on disk is promoted to the
-- vector file. Images without a sibling (photos, renders, screenshots) pass
-- through untouched, so the rule applies itself only to plots that opted in.
local function file_exists(path)
  local f = io.open(path, "r")
  if f then f:close(); return true end
  return false
end

local function resolve_on_resource_path(src)
  if src:match("^/") then
    return file_exists(src) and src or nil
  end
  local dirs = { "." }
  local ok, rp = pcall(function() return PANDOC_STATE.resource_path end)
  if ok and type(rp) == "table" then dirs = rp end
  for _, dir in ipairs(dirs) do
    local cand = tostring(dir) .. "/" .. src
    if file_exists(cand) then return cand end
  end
  -- last resort: next to the first input file
  local ok2, files = pcall(function() return PANDOC_STATE.input_files end)
  if ok2 and files and files[1] then
    local base = tostring(files[1]):match("(.*/)") or "./"
    local cand = base .. src
    if file_exists(cand) then return cand end
  end
  return nil
end

function Image(img)
  if not FORMAT:match("latex") then return nil end
  if not img.src:match("%.png$") then return nil end
  local sibling = img.src:gsub("%.png$", ".pdf")
  local found = resolve_on_resource_path(sibling)
  if found then
    img.src = found
    return img
  end
  return nil
end

-- Two passes: the first marks the tables inside a widths-from-dashes div, the
-- second is the filter described at the top of this file. pandoc visits a
-- table before the div around it, so a single pass could not tell the Table
-- function about the div.
return {
  { Div = mark_widths_from_dashes },
  { Table = Table, Str = Str, Code = Code, RawBlock = RawBlock,
    RawInline = RawInline, Image = Image },
}
