-- filter.lua
-- Pandoc Lua filter for the report. Single filter that:
--
--   (1) Renders every pandoc Table as a raw-LaTeX longtable with:
--         * auto-sized column widths (weight = 2 * longest_unbreakable_token
--           + sqrt(max_content_length); short cols don't get over-allocated,
--           long-text cols don't starve neighbours);
--         * raggedright p{...} columns (no mid-word hyphenation);
--         * faint \hline between body rows (color set in header.tex);
--         * single-bold-cell rows rendered as \multicolumn section labels.
--
--   (2) Rewrites every \texttt{...} cell content (and inline Code nodes
--       in body prose, and \href{}{display} link text) to allow line
--       breaks at identifier boundaries: camelCase humps, ::, _, ., -, /,
--       before (/[/<, after )/]/>. Long all-alphabetic runs (>=14 chars)
--       get last-resort letter-by-letter break opportunities at a high
--       penalty.
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
  if t == "RawInline" then return inl.text or "" end
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
-- Long identifiers won't break to fit narrow columns by default; insert
-- \allowbreak{} at camelCase boundaries and after ::, _, ., -, /, (, <, >.

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
-- A bare `\penalty 9999` is itself a feasible breakpoint -- TeX scans
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
      flush_run()
      table.insert(out, "\\_\\allowbreak{}")
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
    elseif c == "." or c == "-" or c == "/" then
      -- Mid-text separators: break AFTER (separator stays on prev line).
      flush_run()
      -- Exception: don't break after `.` when the trailing run looks like a
      -- file extension (1-5 alpha chars to end-of-string or non-alphanumeric).
      -- Otherwise we get ugly splits like `clouddistancecalculator.` / `cpp`.
      -- The earlier `/` break in a path will be preferred by LaTeX instead.
      local suppress = false
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
      if suppress then
        table.insert(out, c)
      else
        table.insert(out, c .. "\\allowbreak{}")
      end
      i = i + 1
      prev_is_lower = false
    elseif c == "(" or c == "[" then
      -- Open bracket: break BEFORE (bracketed content stays as a unit).
      flush_run()
      table.insert(out, "\\allowbreak{}" .. c)
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
        -- char. Higher penalty than \allowbreak{} (which is \penalty 0) so
        -- separator-based break opportunities (after :_./() etc.) are
        -- strongly preferred. Keeps `.maxCoeff()` from being split as
        -- `.max` | `Coeff()` when a break before the whole token exists.
        flush_run()
        table.insert(out, "\\penalty 300 ")
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
          table.insert(result, "\\texttt{" .. tt_insert_breaks(body) .. "}")
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
  return widths
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
  local widths = compute_widths(hcells, brows, n_cols)

  -- caption: prefer the long caption Inline list
  local caption_tex = ""
  if t.caption and t.caption.long and #t.caption.long > 0 then
    local doc = pandoc.Pandoc(t.caption.long)
    caption_tex = pandoc.write(doc, "latex"):gsub("\n+$", "")
  end

  local col_spec = {}
  for i = 1, n_cols do
    -- \raggedright + \sloppy + finite emergencystretch: the line-breaker is
    -- told both that ragged-right lines are fine AND that it has slack to
    -- avoid overshooting. Without \sloppy + emergencystretch, TeX can
    -- still prefer a high-penalty mid-identifier break over a short, well-
    -- behaved /-break, because the badness model under just \raggedright
    -- doesn't fully neutralise short-line cost in <texttt> cells.
    col_spec[i] = string.format(
      ">{\\raggedright\\sloppy\\emergencystretch=3em\\arraybackslash}p{%.4f\\textwidth}",
      widths[i])
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
      if body_row_count > 0 then table.insert(out, "\\hline") end
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

-- ----- inline Code, RawBlock, RawInline: apply the same break rules ----

-- LaTeX-escape raw user code (from markdown backticks). Mirrors pandoc's
-- own escaping but explicit so we can feed the result through
-- tt_insert_breaks before re-wrapping in \texttt{}.
local function latex_escape(s)
  s = s:gsub("\\", "\\textbackslash{}")
  s = s:gsub("([&%%#%${}_^])", "\\%1")
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
