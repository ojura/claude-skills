from .semantic_support import *


class SelectableTex(SemanticAssertions):
    def test_ligature_and_independent_spaces_have_real_word_selection(self):
        prose = self.scene.prose("office A B", size=48, color="muted")
        leaves = prose.family_members_with_points()
        # A/B are separate visible source glyphs; spaces must not steal their advance.
        a_ink, b_ink = [ink_rectangle(leaf) for leaf in leaves[-2:]]
        path = self.pdf([prose])
        self.assertEqual(flattened(self.poppler_text(path)), "office A B")
        self.assertEqual(self.poppler_words(path), ["office", "A", "B"])
        with fitz.open(path) as doc:
            page = doc[0]
            self.assert_visible_font_text(page)
            self.assertEqual(flattened(page.get_text()), "office A B")
            office = self.assert_search_hits(page, "office")[0]
            self.assertEqual(page.get_textbox(office).strip(), "office")
            self.assert_search_hits(page, "ffi")
            for letter, ink in (("A", a_ink), ("B", b_ink)):
                rectangle = self.assert_search_hits(page, letter)[0]
                self.assertGreater((rectangle & ink).get_area(), ink.get_area() * 0.3)
                self.assertEqual(page.get_textbox(rectangle).strip(), letter)


    def test_sized_matrix_delimiters_keep_source_geometry_and_selectable_characters(self):
        cases = [(r"\mathbf{t}_{WB}=\begin{bmatrix}t_x\\t_y\end{bmatrix}", "[]"),
                 (r"\begin{pmatrix}x\\y\end{pmatrix}", "()")]
        cases += [(rf"\{size}l[ x \{size}r]", "[]")
                  for size in ("big", "Big", "bigg", "Bigg")]
        for source, delimiters in cases:
            with self.subTest(source=source):
                expression = MathTex(source, tex_template=TEMPLATE, font_size=40,
                                     color=PALETTES["light"]["foreground"])
                original_points = expression.get_all_points().copy()
                bind_tex(expression)
                np.testing.assert_array_equal(expression.get_all_points(), original_points)
                leaves = expression.family_members_with_points()
                names = [leaf._tex_glyph.source.glyph_name for leaf in leaves
                         if hasattr(leaf, "_tex_glyph")]
                self.assertTrue(any(re.search(r"(?:big|Big|bigg|Bigg)$", name) for name in names))
                path = self.pdf([expression])
                texts = [self.poppler_text(path)]
                with fitz.open(path) as document:
                    self.assert_visible_font_text(document[0])
                    texts.append(document[0].get_text())
                for text in texts:
                    for character in delimiters:
                        self.assertEqual(text.count(character), 1, (source, text))
                    self.assertIn("x", text)
                    self.assertFalse(any(unicodedata.category(char) == "Co" for char in text))


    def test_same_source_copy_tracks_scale_rotation_and_baseline(self):
        original = self.scene.prose("office A B", size=40).move_to([-2, 1.4, 0])
        original_points = original.get_all_points().copy()
        angle = 0.23
        copied = original.copy().scale(0.7).rotate(angle).move_to([2, -1.3, 0])
        path = self.pdf([original, copied])
        np.testing.assert_array_equal(original.get_all_points(), original_points)
        with fitz.open(path) as doc:
            page = doc[0]
            self.assertEqual(flattened(page.get_text()), "office A B office A B")
            self.assert_search_hits(page, "A", 2)
            quads = page.search_for("office", quads=True)
            self.assertEqual(len(quads), 2)
            quads.sort(key=lambda quad: quad.rect.y0)
            directions = []
            for quad in quads:
                direction = np.array([quad.ur.x - quad.ul.x, quad.ur.y - quad.ul.y])
                directions.append(direction / np.linalg.norm(direction))
            np.testing.assert_allclose(directions[0], [1, 0], atol=2e-3)
            np.testing.assert_allclose(directions[1], [math.cos(angle), -math.sin(angle)], atol=2e-3)
            heights = [math.hypot(quad.ll.x - quad.ul.x, quad.ll.y - quad.ul.y) for quad in quads]
            self.assertAlmostEqual(heights[1] / heights[0], 0.7, delta=0.02)


    def test_ligature_source_slots_preserve_complete_words(self):
        # TeX's OT1 log can contain literal control-byte slots for ff/fi/fl;
        # these must remain glyph nodes rather than being split as new lines.
        words = "office offline affinity offset first flow"
        prose = self.scene.prose(words, size=30)
        path = self.pdf([prose])
        self.assertEqual(flattened(self.poppler_text(path)), words)
        self.assertEqual(self.poppler_words(path), words.split())
        with fitz.open(path) as doc:
            page = doc[0]
            self.assertEqual(flattened(page.get_text()), words)
            for word in words.split():
                self.assert_search_hits(page, word)


    def test_equivalent_fitted_metrics_produce_identical_pdf_graphs(self):
        prose = self.scene.prose("office A B", size=36).move_to([-2, 0, 0])
        arrow = self.scene.math(r"\Longrightarrow", size=36, position=[1.3, 0, 0])
        original = VGroup(prose, arrow)
        first = paths_from_mobjects([prose])[0]["glyph"]
        # Hosted Haswell/Sandybridge fits straddled this Cairo decimal boundary.
        original.scale(21.0668325 / first.em_x[0]).move_to([-1.2, 1.2, 0])
        copied = (original.copy().scale(0.7).rotate(0.23)
                  .apply_matrix([[1, 0.17, 0], [0.08, 1, 0], [0, 0, 1]])
                  .move_to([1.1, -1.2, 0]))
        objects = [original, copied]
        points = [obj.get_all_points().copy() for obj in objects]
        raw = paths_from_mobjects(objects)
        self.assertTrue(any(path["glyph"].spaces_before for path in raw))
        self.assertTrue(any(len(path["glyph"].source.cluster) > 1 for path in raw))
        # Define equivalent fit results independently of the sink's helper.
        # Each pair differs by 4e-12 canvas units; path points and source
        # records stay exactly the same, including the rotated/sheared copy.
        nominal = []
        for path in raw:
            glyph = path["glyph"]
            rounded = lambda values: tuple(round(float(value), 8) for value in values)
            nominal.append(dict(path, glyph=replace(
                glyph, baseline=rounded(glyph.baseline),
                em_x=rounded(glyph.em_x), em_y=rounded(glyph.em_y),
                spaces_before=tuple((space, rounded(origin))
                                    for space, origin in glyph.spaces_before))))
        self.assertEqual(nominal[0]["glyph"].em_x[0], 21.0668325)
        self.assertEqual(nominal[0]["glyph"].em_x[1], 0.0)

        def perturbed(sign, fields):
            def jitter(values):
                return tuple(value + sign * (1 if index == 0 else -1) * 2e-12
                             for index, value in enumerate(values))
            result = []
            for path in nominal:
                glyph = path["glyph"]
                changes = {}
                if "basis" in fields:
                    changes.update(em_x=jitter(glyph.em_x), em_y=jitter(glyph.em_y))
                if "baseline" in fields:
                    changes["baseline"] = jitter(glyph.baseline)
                if "spaces" in fields:
                    changes["spaces_before"] = tuple(
                        (space, jitter(origin)) for space, origin in glyph.spaces_before)
                result.append(dict(path, glyph=replace(glyph, **changes)))
            return result

        reference = self.pdf([], paths=nominal)
        for fields in (("basis",), ("baseline",), ("spaces",),
                       ("basis", "baseline", "spaces")):
            with self.subTest(fields=fields):
                for sign in (-1, 1):
                    fitted = perturbed(sign, fields)
                    snapshots = [path["glyph"] for path in fitted]
                    candidate = self.pdf([], paths=fitted)
                    comparison = compare_pdf(reference, candidate)
                    self.assertTrue(comparison["equal"], comparison["difference"])
                    self.assertEqual([path["glyph"] for path in fitted], snapshots)
                    self.assertEqual([path["coords"] for path in fitted],
                                     [path["coords"] for path in raw])
        with fitz.open(reference) as doc:
            self.assert_visible_font_text(doc[0])
            text = flattened(doc[0].get_text())
            self.assertEqual(text.count("⟹"), 2)
            self.assertEqual(flattened(text.replace("⟹", "")), "office A B office A B")
            for phrase in ("office", "A", "B", "⟹"):
                self.assert_search_hits(doc[0], phrase, 2)
        for obj, expected in zip(objects, points):
            np.testing.assert_array_equal(obj.get_all_points(), expected)
        changed = list(nominal)
        glyph = changed[0]["glyph"]
        changed[0] = dict(changed[0], glyph=replace(
            glyph, baseline=(glyph.baseline[0] + 1e-4, glyph.baseline[1])))
        self.assertFalse(compare_pdf(reference, self.pdf([], paths=changed))["equal"],
                         "a change at the path-coordinate precision must remain observable")


    def test_inline_math_keeps_symbols_and_actual_subscript_baselines(self):
        prose = self.scene.prose(r"Contribution of $x_L$ and $y_L$.", size=34).move_to([0, 1.2, 0])
        formula = self.scene.math(r"x_L+\theta-y_L", size=40, position=[0, -0.8, 0])
        path = self.pdf([prose, formula])
        with fitz.open(path) as doc:
            reader_text = doc[0].get_text()
        for text in (self.poppler_text(path), reader_text):
            joined = "".join(text.split())
            self.assertIn("ContributionofxLandyL.", joined)
            self.assertIn("xL+θ−yL", joined)
        with fitz.open(path) as doc:
            page = doc[0]
            self.assert_search_hits(page, "Contribution")
            self.assert_search_hits(page, "θ")
            self.assert_search_hits(page, "−")
            for leaf in formula.family_members_with_points():
                symbol = snapshot_glyph(leaf).source.copy_text
                if symbol in ("θ", "+", "−"):
                    ink = ink_rectangle(leaf)
                    rectangle = self.assert_search_hits(page, symbol)[0]
                    self.assertGreater((rectangle & ink).get_area(), ink.get_area() * 0.3,
                                       (symbol, rectangle, ink))
            raw = page.get_text("rawdict")
            chars = [char for block in raw["blocks"] if block["type"] == 0
                     for line in block["lines"] for span in line["spans"] for char in span["chars"]
                     if char["origin"][1] > HEIGHT / 2]
            x = next(char for char in chars if char["c"] == "x")
            subscript = next(char for char in chars if char["c"] == "L")
            self.assertGreater(subscript["origin"][1], x["origin"][1] + 0.1)


    def test_colored_fragments_copy_as_one_sentence_in_both_themes(self):
        text = "Each column is one local direction in world coordinates."
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                configure(theme)
                scene = Derivation(PALETTES[theme], sample_rate=10)
                prose = scene.prose(text, 30, "muted",
                                    alert_terms=("column", "local direction", "world coordinates"))
                path = self.pdf([prose], theme=theme)
                self.assertEqual(flattened(self.poppler_text(path)), text)
                with fitz.open(path) as doc:
                    page = doc[0]
                    self.assertEqual(flattened(page.get_text()), text)
                    for phrase in ("column", "local direction", "world coordinates"):
                        self.assert_search_hits(page, phrase)
                    colors = {span["color"] for block in page.get_text("dict")["blocks"]
                              if block["type"] == 0 for line in block["lines"] for span in line["spans"]}
                    # Cairo writes floating RGB; MuPDF quantizes it back to bytes.
                    # One-byte rounding must not hide a missing gray/red text run.
                    decoded = [tuple((color >> shift) & 255 for shift in (16, 8, 0))
                               for color in colors]
                    for role in ("accent", "muted"):
                        wanted = tuple(int(PALETTES[theme][role][i:i + 2], 16)
                                       for i in (1, 3, 5))
                        self.assertTrue(any(all(abs(a - b) <= 1 for a, b in zip(rgb, wanted))
                                            for rgb in decoded), (role, wanted, decoded))


    def test_hidden_removed_and_outside_crop_text_is_absent(self):
        kept = self.scene.prose("visible caption", 34, position=[-3, 0.5, 0])
        hidden = self.scene.prose("hidden caption", 34, position=[-3, -1, 0]).set_opacity(0)
        outside = self.scene.prose("outside caption", 34, position=[3, 0.5, 0])
        removed = self.scene.prose("removed caption", 34, position=[-3, 2, 0])
        self.scene.add(kept, hidden, outside, removed)
        self.scene.remove(removed)
        path = self.pdf([], paths=self.scene.current_paths(), crop=[0, 0, WIDTH / 2, HEIGHT])
        self.assertEqual(flattened(self.poppler_text(path)), "visible caption")
        with fitz.open(path) as doc:
            page = doc[0]
            self.assertEqual(flattened(page.get_text()), "visible caption")
            for absent in ("hidden", "outside", "removed"):
                self.assertFalse(page.search_for(absent), absent)


    def test_crop_between_words_of_one_tex_object_preserves_only_visible_glyphs(self):
        prose = self.scene.prose("keep drop", 40, position=[-2, 0, 0])
        leaves = prose.family_members_with_points()
        self.assertEqual(len(leaves), 8)
        left, right = ink_rectangle(leaves[3]), ink_rectangle(leaves[4])
        self.assertLess(left.x1, right.x0)
        edge = (left.x1 + right.x0) / 2
        path = self.pdf([prose], crop=[0, 0, edge, HEIGHT])
        self.assertEqual(flattened(self.poppler_text(path)), "keep")
        self.assertEqual(self.poppler_words(path), ["keep"])
        with fitz.open(path) as doc:
            page = doc[0]
            self.assertEqual(flattened(page.get_text()), "keep")
            self.assertFalse(page.search_for("drop"))
            rectangle = self.assert_search_hits(page, "keep")[0]
            self.assertLessEqual(rectangle.x1, page.rect.x1 + 0.01)
            self.assertEqual(page.get_textbox(rectangle).strip(), "keep")


    def test_unbound_tex_in_nested_groups_fails_then_explicit_binding_succeeds(self):
        for route in ("paths_from_mobjects", "current_paths"):
            with self.subTest(route=route):
                # Use ordinary Manim classes deliberately: a future author must
                # not silently lose semantics by bypassing Derivation helpers.
                raw_prose = Tex(r"\textsf{plain text}", tex_template=TEMPLATE,
                                font_size=36).move_to([0, 1, 0])
                raw_math = MathTex(r"x_L+\theta", tex_template=TEMPLATE,
                                   font_size=36).move_to([0, -1, 0])
                group = VGroup(VGroup(raw_prose, raw_math), Line([-3, -2, 0], [3, -2, 0]))
                scene = Derivation(PALETTES["light"], sample_rate=10)
                scene.add(group)
                def capture():
                    if route == "paths_from_mobjects":
                        return paths_from_mobjects([group])
                    return scene.current_paths()
                with self.assertRaisesRegex(TextMappingError, "bind_tex"):
                    capture()
                bind_tex(raw_prose)
                bind_tex(raw_math)
                paths = capture()
                path = self.pdf([], paths=paths)
                with fitz.open(path) as doc:
                    self.assert_visible_font_text(doc[0])
                    self.assertEqual(flattened(doc[0].get_text()).replace(" ", ""), "plaintextxL+θ")
                self.assertEqual("".join(self.poppler_text(path).split()), "plaintextxL+θ")
                detached = raw_prose.family_members_with_points()[0].copy()
                self.assertEqual(flattened(self.poppler_text(self.pdf([detached]))), "p")


    def test_description_sequence_rejects_skipped_or_invisible_steps(self):
        labels = ("First visible step", "Second visible step")
        descriptions = StepDescriptions(
            self.scene, labels,
            lambda label, index: self.scene.prose(label, position=[0, -index, 0]))
        descriptions.begin()
        descriptions.checkpoint()
        self.assertEqual(self.scene.checkpoints[0]["label"], labels[0])
        with self.assertRaisesRegex(ValueError, "Every declared teaching description"):
            descriptions.finish()
        self.scene.remove(descriptions.items[0])
        with self.assertRaisesRegex(ValueError, "missing or transparent"):
            descriptions.checkpoint()
        self.scene.add(descriptions.items[0])
        descriptions.items[0].set_opacity(0)
        with self.assertRaisesRegex(ValueError, "missing or transparent"):
            descriptions.checkpoint()


    def test_source_font_angle_and_program_identity_survive_repeated_compiler_probes(self):
        font_map = select_font_map(str(self.folder))
        cache = str(self.folder / "font metadata # repeat")
        expected = {"rm-lmss10": (0.0, "LMSans10-Regular"),
                    "lmmi10": (-14.0362, "LMMathItalic10-Regular")}
        observed = {}
        for family, (angle, name) in expected.items():
            with self.subTest(family=family):
                first = probe_font_program(family, font_map, cache)
                probe_font_program.cache_clear()
                repeated = probe_font_program(family, font_map, cache)
                self.assertEqual(first, repeated)
                self.assertAlmostEqual(first["italic_angle"], angle, places=6)
                self.assertEqual(first["postscript_name"], name)
                self.assertRegex(first["program_sha256"], r"^[0-9a-f]{64}$")
                observed[family] = first
        self.assertNotEqual(observed["rm-lmss10"]["program_sha256"],
                            observed["lmmi10"]["program_sha256"])
        for text in (self.scene.prose("Known poses", 30), self.scene.math("x", 30)):
            font = snapshot_glyph(text.family_members_with_points()[0]).source.font
            self.assertEqual(font.italic_angle, observed[font.family]["italic_angle"])
            self.assertEqual(font.postscript_name, observed[font.family]["postscript_name"])
            self.assertEqual(font.program_sha256, observed[font.family]["program_sha256"])


    def test_synthetic_font_maps_fail_explicitly(self):
        for name, instructions in (("slant", "0.2 SlantFont"),
                                   ("extend", "0.8 ExtendFont"),
                                   ("both", "0.2 SlantFont 0.8 ExtendFont")):
            with self.subTest(mapping=name):
                font_map = self.folder / f"{name} source # map.map"
                font_map.write_text(
                    f'rm-lmss10 LMSans10-Regular "{instructions}" <lmss10.pfb\n')
                with self.assertRaisesRegex(FontSourceError, "FontMatrix"):
                    probe_font_program("rm-lmss10", str(font_map),
                                       str(self.folder / "synthetic fonts"))


    def test_changed_font_map_refreshes_existing_probe_products(self):
        font_map = self.folder / "changed source # map.map"
        cache = str(self.folder / "changed font cache")
        font_map.write_text("rm-lmss10 LMSans10-Regular <lmss10.pfb\n")
        first = probe_font_program("rm-lmss10", str(font_map), cache)
        # Clearing the process cache represents a later authoring process using
        # the same external probe directory after its selected font map changed.
        font_map.write_text("rm-lmss10 LMSans8-Regular <lmss8.pfb\n")
        probe_font_program.cache_clear()
        changed = probe_font_program("rm-lmss10", str(font_map), cache)
        self.assertEqual(first["postscript_name"], "LMSans10-Regular")
        self.assertEqual(changed["postscript_name"], "LMSans8-Regular")
        self.assertNotEqual(first["program_sha256"], changed["program_sha256"])
        probe_font_program.cache_clear()


    def test_layout_cache_refreshes_metadata_with_identical_glyph_outlines(self):
        case = self.folder / "layout-cache-metadata"
        seed = case / "seed"
        original_map = select_font_map(str(self.folder))
        probe_font_program("rm-lmss10", original_map, str(seed))
        seed_pdfs = list((seed / "tex-font-programs").glob("*/font.pdf"))
        self.assertEqual(len(seed_pdfs), 1)
        reader = PdfReader(seed_pdfs[0], strict=True)
        font = next(iter(reader.pages[0]["/Resources"]["/Font"].values())).get_object()
        stream = font["/FontDescriptor"]["/FontFile"]
        program = stream.get_data()
        lengths = [int(stream.get(f"/Length{index}", 0)) for index in (1, 2, 3)]
        self.assertGreater(min(lengths[:2]), 0)
        self.assertEqual(sum(lengths), len(program))
        header = program[:lengths[0]]
        encrypted = program[lengths[0]:lengths[0] + lengths[1]]
        trailer = program[lengths[0] + lengths[1]:] or b"\n" + b"0" * 512 + b"\ncleartomark\n"
        header, count = re.subn(rb"^/FontName\s+/[^\s]+\s+def\s*$",
                               b"/FontName /PSRMetadataControl def", header, flags=re.MULTILINE)
        self.assertEqual(count, 1)

        def pfb(angle):
            metadata, count = re.subn(rb"^/ItalicAngle\s+[-+0-9.]+\s+def\s*$",
                                      b"/ItalicAngle " + str(angle).encode() + b" def",
                                      header, flags=re.MULTILINE)
            self.assertEqual(count, 1)
            # PFB has typed, length-prefixed segments. Both fixtures retain the
            # exact encrypted glyph program and trailer; only the angle changes.
            return b"".join(bytes((128, kind)) + struct.pack("<I", len(data)) + data
                            for kind, data in ((1, metadata), (2, encrypted), (1, trailer))) + b"\x80\x03"

        active_font = case / "psr-metadata-control.pfb"
        active_font.write_bytes(pfb(0))
        local_map = case / "source.map"
        local_map.write_text("rm-lmss10 PSRMetadataControl <psr-metadata-control.pfb\n")
        media = case / "media"
        (media / "tex").mkdir(parents=True)
        environment = {"T1FONTS": str(case) + os.pathsep + os.environ.get("T1FONTS", "")}
        try:
            with tempconfig({"media_dir": str(media), "tex_dir": str(media / "tex")}), \
                    patch.dict(os.environ, environment), \
                    patch.object(tex_semantics, "select_font_map", return_value=str(local_map)):
                tex_semantics._layout.cache_clear()
                probe_font_program.cache_clear()
                original = Tex(r"\textsf{A}", tex_template=TEMPLATE, font_size=36)
                original_points = original.get_all_points().copy()
                original_svg = Path(original.file_name).read_bytes()
                bind_tex(original)
                first = snapshot_glyph(original.family_members_with_points()[0]).source
                self.assertEqual(first.font.italic_angle, 0)
                layouts = list((media / "tex-semantics").glob("*/layout.json"))
                self.assertEqual(len(layouts), 1)
                cached = layouts[0].read_bytes()
                active_font.write_bytes(pfb(-7.5))
                # Keep the on-disk layout; simulate a later authoring process.
                tex_semantics._layout.cache_clear()
                probe_font_program.cache_clear()
                refreshed = Tex(r"\textsf{A}", tex_template=TEMPLATE, font_size=36)
                bind_tex(refreshed)
                second = snapshot_glyph(refreshed.family_members_with_points()[0]).source
                np.testing.assert_array_equal(original.get_all_points(), original_points)
                np.testing.assert_array_equal(refreshed.get_all_points(), original_points)
                self.assertEqual(Path(refreshed.file_name).read_bytes(), original_svg)
                self.assertEqual(second.font.italic_angle, -7.5)
                self.assertEqual(first.font.postscript_name, "PSRMetadataControl")
                self.assertEqual(second.font.postscript_name, first.font.postscript_name)
                self.assertNotEqual(first.font.program_sha256, second.font.program_sha256)
                self.assertNotEqual(first.layout_key, second.layout_key)
                self.assertEqual(list((media / "tex-semantics").glob("*/layout.json")), layouts)
                self.assertNotEqual(layouts[0].read_bytes(), cached)
        finally:
            tex_semantics._layout.cache_clear()
            probe_font_program.cache_clear()


    def test_glyph_table_changes_refresh_fallback_layouts_and_reject_unknown_mappings(self):
        case = self.folder / "glyph-source-cache"
        media = case / "media"
        (media / "tex").mkdir(parents=True)
        table = case / "glyphtounicode.tex"
        mappings = (r"\pdfglyphtounicode{bracketleft}{005B}" + "\n" +
                    r"\pdfglyphtounicode{bracketright}{005D}" + "\n")
        table.write_text(mappings)
        environment = {"TEXINPUTS": str(case) + os.pathsep + os.environ.get("TEXINPUTS", "")}
        source = r"\begin{bmatrix}x\\y\end{bmatrix}"

        def clear_process_cache():
            tex_semantics._layout.cache_clear()
            probe_glyph_unicode_map.cache_clear()

        def binding(text):
            return snapshot_glyph(text.family_members_with_points()[0]).source

        try:
            with tempconfig({"media_dir": str(media), "tex_dir": str(media / "tex")}), \
                    patch.dict(os.environ, environment):
                clear_process_cache()
                matrix = MathTex(source, tex_template=TEMPLATE, font_size=36)
                points = matrix.get_all_points().copy()
                svg = Path(matrix.file_name).read_bytes()
                bind_tex(matrix)
                first = binding(matrix)
                ordinary = MathTex("x", tex_template=TEMPLATE, font_size=36)
                bind_tex(ordinary)
                ordinary_key = binding(ordinary).layout_key
                layouts = {json.loads(path.read_text())["key"]: path
                           for path in (media / "tex-semantics").glob("*/layout.json")}
                fallback_path, ordinary_path = layouts[first.layout_key], layouts[ordinary_key]
                original_fallback, original_ordinary = (path.read_bytes()
                                                        for path in (fallback_path, ordinary_path))
                expected_source = {"file": str(table),
                                   "sha256": hashlib.sha256(table.read_bytes()).hexdigest()}
                self.assertEqual(json.loads(original_fallback)["glyph_unicode_source"], expected_source)
                self.assertIsNone(json.loads(original_ordinary)["glyph_unicode_source"])

                # A later authoring process must revalidate the observed table,
                # even when its changed contents preserve the relevant mappings.
                table.write_text(mappings + "% revised source table\n")
                clear_process_cache()
                refreshed = MathTex(source, tex_template=TEMPLATE, font_size=36)
                bind_tex(refreshed)
                second = binding(refreshed)
                self.assertNotEqual(first.layout_key, second.layout_key)
                updated = json.loads(fallback_path.read_text())
                self.assertEqual(updated["key"], second.layout_key)
                self.assertEqual(updated["glyph_unicode_source"]["sha256"],
                                 hashlib.sha256(table.read_bytes()).hexdigest())
                self.assertNotEqual(fallback_path.read_bytes(), original_fallback)
                np.testing.assert_array_equal(refreshed.get_all_points(), points)
                self.assertEqual(Path(refreshed.file_name).read_bytes(), svg)
                ordinary = MathTex("x", tex_template=TEMPLATE, font_size=36)
                bind_tex(ordinary)
                self.assertEqual(binding(ordinary).layout_key, ordinary_key)
                self.assertEqual(ordinary_path.read_bytes(), original_ordinary)

                valid_cache = fallback_path.read_bytes()
                for left_mapping in ("", r"\pdfglyphtounicode{bracketleft}{E014}"):
                    with self.subTest(left_mapping=left_mapping):
                        table.write_text(left_mapping + "\n" +
                                         r"\pdfglyphtounicode{bracketright}{005D}" + "\n")
                        clear_process_cache()
                        rejected = MathTex(source, tex_template=TEMPLATE, font_size=36)
                        with self.assertRaisesRegex(TextMappingError, "no supported Unicode mapping"):
                            bind_tex(rejected)
                        self.assertTrue(all(snapshot_glyph(leaf) is None
                                            for leaf in rejected.family_members_with_points()))
                        np.testing.assert_array_equal(rejected.get_all_points(), points)
                        self.assertEqual(fallback_path.read_bytes(), valid_cache)
                np.testing.assert_array_equal(matrix.get_all_points(), points)
        finally:
            clear_process_cache()


    def test_joined_long_arrow_is_distinct_from_an_ordinary_equals_and_arrow(self):
        joined = self.scene.math(r"\Longrightarrow", 42, position=[0, 1, 0])
        distinct = self.scene.math(r"=\Rightarrow", 42, position=[0, -1, 0])
        path = self.pdf([joined, distinct])
        with fitz.open(path) as doc:
            text = "".join(doc[0].get_text().split())
            self.assertEqual(text, "⟹=⇒")
        self.assertEqual("".join(self.poppler_text(path).split()), "⟹=⇒")


    def test_mapsto_operators_keep_source_outlines_and_one_character_per_copy(self):
        for command, character, names in (
                (r"\mapsto", "↦", ("mapsto", "arrowright")),
                (r"\longmapsto", "⟼", ("mapsto", "minus", "arrowright"))):
            with self.subTest(command=command):
                original = MathTex(command, tex_template=TEMPLATE, font_size=42,
                                   color=PALETTES["light"]["foreground"]).move_to([-2, 1, 0])
                points = original.get_all_points().copy()
                bind_tex(original)
                np.testing.assert_array_equal(original.get_all_points(), points)
                sources = [snapshot_glyph(leaf).source
                           for leaf in original.family_members_with_points()]
                self.assertEqual(tuple(source.glyph_name for source in sources), names)
                self.assertEqual(sources[0].advance, 0)
                for source in sources:
                    self.assertEqual(source.cluster, tuple(range(len(names))))
                    self.assertEqual(source.cluster_text, character)
                    self.assertEqual(source.cluster_base, len(names) - 1)
                copied = original.copy().scale(0.75).rotate(0.18).move_to([2, -1, 0])
                copied_points = copied.get_all_points().copy()
                path = self.pdf([original, copied])
                self.assertEqual("".join(self.poppler_text(path).split()), character * 2)
                with fitz.open(path) as document:
                    self.assert_visible_font_text(document[0])
                    self.assertEqual("".join(document[0].get_text().split()), character * 2)
                    self.assert_search_hits(document[0], character, 2)
                np.testing.assert_array_equal(original.get_all_points(), points)
                np.testing.assert_array_equal(copied.get_all_points(), copied_points)


    def test_mapsto_source_markers_preserve_unmarked_neighbouring_operators(self):
        formula = self.scene.math(
            r"\mapsto\quad\mapstochar\rightarrow\quad"
            r"\longmapsto\quad\mapstochar\longrightarrow", size=42)
        path = self.pdf([formula])
        expected = "↦↦→⟼↦⟶"
        self.assertEqual("".join(self.poppler_text(path).split()), expected)
        with fitz.open(path) as document:
            self.assertEqual("".join(document[0].get_text().split()), expected)


    def test_composite_provenance_distinguishes_occurrences_in_one_expression(self):
        # Both pairs have a negative join kern. Only the authored long operator
        # may become one character; a global source-substring test is insufficient.
        formula = self.scene.math(r"\Longrightarrow\quad=\!\Rightarrow", size=42)
        path = self.pdf([formula])
        self.assertEqual("".join(self.poppler_text(path).split()), "⟹=⇒")
        with fitz.open(path) as doc:
            page = doc[0]
            self.assertEqual("".join(page.get_text().split()), "⟹=⇒")
            self.assert_search_hits(page, "⟹")
            self.assert_search_hits(page, "=")
            self.assert_search_hits(page, "⇒")


    def test_source_binding_is_idempotent_and_affine_transform_is_supported(self):
        text = Tex(r"\textsf{office}", tex_template=TEMPLATE, font_size=36)
        points = text.get_all_points().copy()
        bind_tex(text)
        bind_tex(text)
        np.testing.assert_array_equal(text.get_all_points(), points)
        self.assertTrue(all(snapshot_glyph(leaf) is not None
                            for leaf in text.family_members_with_points()))
        target = text.copy().scale(0.8).rotate(0.2).shift([1, -0.5, 0])
        copied = [snapshot_glyph(leaf) for leaf in target.family_members_with_points()]
        bind_tex(target)
        for leaf, prior in zip(target.family_members_with_points(), copied):
            current = snapshot_glyph(leaf)
            self.assertIs(current.source, prior.source)
            self.assertIs(current.instance, prior.instance)
        validate_text_transform(Transform(text, target))
        self.scene.add(text)
        self.scene.play(Transform(text, target), run_time=0.1)
        path = self.pdf([text])
        self.assertEqual(flattened(self.poppler_text(path)), "office")


    def test_copied_composite_keeps_two_independent_operator_instances(self):
        original = self.scene.math(r"\Longrightarrow", size=42, position=[-2, 1, 0])
        copied = original.copy().scale(0.75).rotate(0.18).move_to([2, -1, 0])
        path = self.pdf([original, copied])
        self.assertEqual("".join(self.poppler_text(path).split()), "⟹⟹")
        with fitz.open(path) as doc:
            self.assertEqual("".join(doc[0].get_text().split()), "⟹⟹")
            self.assert_search_hits(doc[0], "⟹", 2)


    def test_rebinding_reordered_leaves_preserves_original_glyph_identities(self):
        text = self.scene.prose("AB", size=36)
        leaves = text.family_members_with_points()
        self.assertEqual(len(leaves), 2)
        sources = [snapshot_glyph(leaf).source for leaf in leaves]
        self.assertEqual([source.copy_text for source in sources], ["A", "B"])
        bind_tex(text)
        argument = text.submobjects[0]
        self.assertEqual(len(argument.submobjects), 2)
        self.assertTrue(all(a is b for a, b in zip(argument.submobjects, leaves)))
        argument.submobjects.reverse()
        with self.assertRaises(TextMappingError):
            bind_tex(text)
        for leaf, source in zip(leaves, sources):
            self.assertIs(snapshot_glyph(leaf).source, source,
                          "a rejected rebind must not relabel an existing glyph")


    def test_first_binding_rejects_reordered_raw_leaves_without_partial_metadata(self):
        raw = Tex(r"\textsf{AB}", tex_template=TEMPLATE, font_size=36)
        leaves = raw.family_members_with_points()
        self.assertEqual(len(leaves), 2)
        raw.submobjects[0].submobjects.reverse()
        with self.assertRaises(TextMappingError):
            bind_tex(raw)
        self.assertTrue(all(snapshot_glyph(leaf) is None for leaf in leaves))


    def test_binding_rejects_mixed_bound_and_unbound_leaves_atomically(self):
        text = self.scene.prose("AB", size=36)
        a, b = text.family_members_with_points()
        a_source = snapshot_glyph(a).source
        raw = Tex(r"\textsf{AB}", tex_template=TEMPLATE, font_size=36)
        replacement = raw.family_members_with_points()[1]
        self.assertIsNone(snapshot_glyph(replacement))
        text.submobjects[0].submobjects[1] = replacement
        with self.assertRaises(TextMappingError):
            bind_tex(text)
        self.assertIs(snapshot_glyph(a).source, a_source)
        self.assertIsNone(snapshot_glyph(replacement))
        self.assertEqual(snapshot_glyph(b).source.copy_text, "B")


    def test_incompatible_content_topology_and_non_affine_changes_fail_explicitly(self):
        original = self.scene.prose("alpha", size=36)
        with self.assertRaises(TextMappingError):
            validate_text_transform(Transform(original, self.scene.prose("beta", size=36)))
        self.scene.add(original)
        with self.assertRaises(TextMappingError):
            self.scene.play(Transform(original, self.scene.prose("beta", size=36)), run_time=0.1)
        for change in ("topology", "non-affine"):
            with self.subTest(change=change):
                glyph = self.scene.math("Q", size=36).family_members_with_points()[0]
                points = glyph.points.copy()
                if change == "topology":
                    glyph.set_points(points[:-4])
                else:
                    points[len(points) // 2, 0] += 0.2
                    glyph.set_points(points)
                with self.assertRaises(TextMappingError):
                    snapshot_glyph(glyph)


    def test_partial_or_inconsistent_composite_operator_fails_explicitly(self):
        for command, count in ((r"\Longrightarrow", 2), (r"\mapsto", 2), (r"\longmapsto", 3)):
            with self.subTest(command=command):
                arrow = self.scene.math(command, size=42)
                leaves = arrow.family_members_with_points()
                self.assertEqual(len(leaves), count)
                for missing in range(count):
                    with self.assertRaises(TextMappingError):
                        self.pdf(leaves[:missing] + leaves[missing + 1:])
                changed = arrow.copy()
                changed.family_members_with_points()[0].set_color(PALETTES["light"]["accent"])
                with self.assertRaises(TextMappingError):
                    self.pdf([changed])


    def test_composite_descriptor_uses_declared_source_base_and_rejects_invalid_bases(self):
        arrow = self.scene.math(r"\Longrightarrow", size=42)
        sources = [snapshot_glyph(leaf).source for leaf in arrow.family_members_with_points()]
        self.assertEqual([source.unicode for source in sources], ["=", "⇒"])
        self.assertEqual([source.cluster_base for source in sources],
                         [sources[1].index, sources[1].index])
        self.assertNotEqual(sources[0].font.italic_angle, sources[1].font.italic_angle)
        path = self.pdf([arrow])
        reader = PdfReader(path, strict=True)
        fonts = list(reader.pages[0]["/Resources"]["/Font"].values())
        self.assertEqual(len(fonts), 1)
        descriptor = fonts[0].get_object()["/FontDescriptor"]
        self.assertAlmostEqual(float(descriptor["/ItalicAngle"]),
                               sources[1].font.italic_angle, places=6)
        self.assertEqual(flattened(self.poppler_text(path)), "⟹")
        previous = path.read_bytes()
        original_paths = paths_from_mobjects([arrow])
        for mode in ("outside", "inconsistent"):
            with self.subTest(base=mode):
                changed = []
                for item in original_paths:
                    glyph = item.get("glyph")
                    if glyph is not None:
                        if mode == "outside":
                            base = max(glyph.source.cluster) + 1
                        else:
                            base = glyph.source.index
                        glyph = replace(glyph, source=replace(glyph.source, cluster_base=base))
                    changed.append(dict(item, glyph=glyph))
                with self.assertRaises(TextMappingError):
                    write_snapshot(path, changed, [1, 1, 1, 1])
                self.assertEqual(path.read_bytes(), previous)
                self.assertFalse(list(self.folder.glob(".glyph-*.pdf")))


    def test_fractional_glyph_opacity_and_rejected_composite_preserve_output(self):
        prose = self.scene.prose("office A B", size=40).set_color("#000000").set_opacity(0.4)
        path = self.pdf([prose])
        self.assertEqual(flattened(self.poppler_text(path)), "office A B")
        with fitz.open(path) as doc:
            page = doc[0]
            self.assertEqual(flattened(page.get_text()), "office A B")
            self.assert_search_hits(page, "office")
            # A solid part of black ink at alpha .4 over white is 60% gray.
            # This detects a dropped alpha without depending on edge antialiasing.
            darkest = min(page.get_pixmap().samples)
            self.assertGreaterEqual(darkest, 151)
            self.assertLessEqual(darkest, 155)
        previous = path.read_bytes()
        original = self.scene.math(r"\Longrightarrow", size=42)
        translucent = original.copy().set_opacity(0.4)
        with self.assertRaisesRegex(TextMappingError, "translucent composite"):
            write_snapshot(path, paths_from_mobjects([translucent]), [1, 1, 1, 1])
        self.assertEqual(path.read_bytes(), previous,
                         "a rejected semantic export must preserve the existing PDF")


    def test_type3_bounds_contain_serialized_cubic_ink_in_transformed_copies(self):
        original = self.scene.math(r"\cos\theta_B+\sin\theta_B", size=42,
                                   position=[-2, 1.4, 0])
        copied = original.copy().scale(0.7).rotate(0.23)
        copied.apply_matrix([[1, 0.15, 0], [0, 1, 0], [0, 0, 1]])
        copied.move_to([2, -0.2, 0])
        mobjects = [original, copied]
        before = [item.get_all_points().copy() for item in mobjects]
        path = self.pdf(mobjects)
        reader = PdfReader(path, strict=True)
        count = zero_lower_edges = 0
        for font_reference in reader.pages[0]["/Resources"]["/Font"].values():
            font = font_reference.get_object()
            font_box = list(map(float, font["/FontBBox"]))
            for name, reference in font["/CharProcs"].items():
                program = reference.get_object()
                ink = serialized_glyph_ink(program, reader)
                if ink is None:
                    continue
                zero_lower_edges += abs(ink[1]) < 1e-12
                header, operator = ContentStream(program, reader).operations[0]
                self.assertEqual(operator, b"d1")
                glyph_box = list(map(float, header[2:]))
                for label, box in (("d1", glyph_box), ("FontBBox", font_box)):
                    with self.subTest(font=font_reference.idnum, glyph=name, bounds=label):
                        self.assertLess(box[0], ink[0] - 1e-7)
                        self.assertLess(box[1], ink[1] - 1e-7)
                        self.assertGreater(box[2], ink[2] + 1e-7)
                        self.assertGreater(box[3], ink[3] + 1e-7)
                count += 1
        self.assertGreater(count, 20)
        # The reported B baseline edge is exactly on the serialization grid;
        # strict inequalities above must cover that case, not only underbounds.
        self.assertGreaterEqual(zero_lower_edges, 2)
        for mobject, points in zip(mobjects, before):
            np.testing.assert_array_equal(mobject.get_all_points(), points)
        self.assertEqual(self.poppler_text(path).count("θ"), 4)
        with fitz.open(path) as doc:
            self.assertEqual(doc[0].get_text().count("θ"), 4)
            self.assert_visible_font_text(doc[0])


    def test_stroke_only_glyph_rejects_atomically_while_fill_plus_stroke_stays_supported(self):
        filled = self.scene.math("B", size=42).set_stroke(color="#333333", width=0.8, opacity=1)
        destination = self.pdf([filled])
        previous = destination.read_bytes()
        self.assertEqual(flattened(self.poppler_text(destination)), "B")
        with fitz.open(destination) as doc:
            self.assertEqual(flattened(doc[0].get_text()), "B")
            self.assert_visible_font_text(doc[0])
            self.assertTrue(any(drawing["type"] in ("s", "fs") and drawing["width"] > 0
                                for drawing in doc[0].get_drawings()))
        stroked = filled.copy().set_fill(opacity=0)
        with self.assertRaisesRegex(TextMappingError, "stroke-only semantic glyph"):
            write_snapshot(destination, paths_from_mobjects([stroked]), [1, 1, 1, 1])
        self.assertEqual(destination.read_bytes(), previous)
        self.assertFalse(list(self.folder.glob(".glyph-*.pdf")))


    def test_font_normalization_preserves_drawing_streams_and_publishes_source_text(self):
        original = self.scene.prose("office A B", size=40).move_to([-2, 1.4, 0])
        copied = original.copy().scale(0.7).rotate(0.23).move_to([2, -1.3, 0])
        real_normalize = pdf_glyphs._normalize_font_metadata
        captured = {}
        def capture(data, emissions, **kwargs):
            captured["pdf"] = data
            captured["emissions"] = {key: replace(value) for key, value in emissions.items()}
            return real_normalize(data, emissions, **kwargs)
        with patch.object(pdf_glyphs, "_normalize_font_metadata", side_effect=capture):
            path = self.pdf([original, copied])
        final = path.read_bytes()
        self.assertTrue(final.startswith(captured["pdf"]))
        before = PdfReader(BytesIO(captured["pdf"]), strict=True)
        after = PdfReader(BytesIO(final), strict=True)
        self.assertEqual(before.pages[0].get_contents().get_data(),
                         after.pages[0].get_contents().get_data())
        old_fonts = before.pages[0]["/Resources"]["/Font"]
        new_fonts = after.pages[0]["/Resources"]["/Font"]
        self.assertEqual(set(old_fonts), set(new_fonts))

        def pdf_bytes(value):
            output = BytesIO()
            value.write_to_stream(output)
            return output.getvalue()

        def matrix(values):
            a, b, c, d, e, f = map(float, values)
            return np.array([[a, c, e], [b, d, f], [0, 0, 1]])

        published = []
        for name in old_fonts:
            old, new = old_fonts[name].get_object(), new_fonts[name].get_object()
            names, next_code = {}, None
            for value in old["/Encoding"]["/Differences"]:
                if isinstance(value, int):
                    next_code = value
                else:
                    names[next_code] = value
                    next_code += 1
            source_by_glyph = {}
            for operands, operator in ContentStream(old["/ToUnicode"], before).operations:
                if operator == b"endbfchar":
                    for code, value in zip(operands[::2], operands[1::2]):
                        source_by_glyph[names[code.original_bytes[0]]] = captured["emissions"][
                            value.original_bytes.decode("utf-16-be")]
            source_records = list(source_by_glyph.values())
            self.assertTrue(source_records)
            self.assertEqual(pdf_bytes(old_fonts.raw_get(name)), pdf_bytes(new_fonts.raw_get(name)))
            self.assertEqual(set(new), set(old) | {"/FontDescriptor"})
            for field in set(old) - {"/FontMatrix", "/Widths", "/FontBBox"}:
                self.assertEqual(pdf_bytes(old.raw_get(field)), pdf_bytes(new.raw_get(field)), field)
            np.testing.assert_allclose(matrix(new["/FontMatrix"]) @ np.diag([1000, -1000, 1]),
                                       matrix(old["/FontMatrix"]), rtol=0, atol=1e-12)
            np.testing.assert_allclose(np.asarray(new["/Widths"], dtype=float) / 1000,
                                       np.asarray(old["/Widths"], dtype=float), rtol=0, atol=1e-12)
            self.assertEqual(float(new["/FontBBox"][0]), 1000 * min(
                float(old["/FontBBox"][0]), *(item.bounds[0] for item in source_records)))
            self.assertEqual(float(new["/FontBBox"][2]), 1000 * max(
                float(old["/FontBBox"][2]), *(item.bounds[2] for item in source_records)))
            self.assertEqual(set(old["/CharProcs"]), set(new["/CharProcs"]))
            for glyph in old["/CharProcs"]:
                old_reference = old["/CharProcs"].raw_get(glyph)
                new_reference = new["/CharProcs"].raw_get(glyph)
                self.assertEqual(pdf_bytes(old_reference), pdf_bytes(new_reference))
                old_program, new_program = old_reference.get_object(), new_reference.get_object()
                old_header, separator, old_body = old_program.get_data().partition(b"\n")
                self.assertEqual(separator, b"\n")
                old_fields = old_header.split()
                operator = old_fields[-1]
                self.assertIn(operator, (b"d0", b"d1"))
                values = [Decimal(value.decode("ascii")) for value in old_fields[:-1]]
                self.assertEqual(len(values), 2 if operator == b"d0" else 6)
                expected = [values[0] * 1000, -values[1] * 1000]
                if operator == b"d1":
                    bounds = [Decimal(str(value)) for value in source_by_glyph[glyph].bounds]
                    expected += [bounds[0] * 1000, -bounds[3] * 1000,
                                 bounds[2] * 1000, -bounds[1] * 1000]
                new_header, separator, new_body = new_program.get_data().partition(b"\n")
                self.assertEqual(separator, b"\n")
                new_fields = new_header.split()
                self.assertEqual(new_fields[-1], operator)
                self.assertEqual([Decimal(value.decode("ascii")) for value in new_fields[:-1]],
                                 expected)
                self.assertEqual(new_body, b"q\n1000 0 0 -1000 0 0 cm\n" + old_body + b"\nQ\n")
                old_operations = ContentStream(old_program, before).operations
                new_operations = ContentStream(new_program, after).operations
                self.assertEqual(new_operations[0][1], old_operations[0][1])
                self.assertEqual(new_operations[1], ([], b"q"))
                self.assertEqual(new_operations[2], ([1000, 0, 0, -1000, 0, 0], b"cm"))
                self.assertEqual(new_operations[3:-1], old_operations[1:])
                self.assertEqual(new_operations[-1], ([], b"Q"))
            expected_angles = {item.base_key[2][4] for item in source_records}
            self.assertEqual(len(expected_angles), 1)
            descriptor = new["/FontDescriptor"]
            self.assertEqual(descriptor["/Type"], "/FontDescriptor")
            self.assertEqual(float(descriptor["/ItalicAngle"]), expected_angles.pop())
            source_y1 = min(min(item.bounds[1] for item in source_records),
                            -max(line[2] for item in source_records for line in item.line_key))
            source_y2 = max(max(item.bounds[3] for item in source_records),
                            max(line[3] for item in source_records for line in item.line_key))
            self.assertAlmostEqual(float(descriptor["/Ascent"]), -1000 * source_y1, places=6)
            self.assertAlmostEqual(float(descriptor["/Descent"]), -1000 * source_y2, places=6)
            # PDF strings may be hexadecimal or literal with octal escapes.
            # Parse both forms before comparing every active mapping to source.
            blocks = [operands for operands, operator in
                      ContentStream(new["/ToUnicode"], after).operations
                      if operator == b"endbfchar"]
            self.assertTrue(blocks)
            codes = []
            for block in blocks:
                self.assertEqual(len(block) % 2, 0)
                for code, value in zip(block[::2], block[1::2]):
                    codes.append(code.original_bytes)
                    text = value.original_bytes.decode("utf-16-be")
                    self.assertTrue(text)
                    self.assertFalse(any(unicodedata.category(char) == "Co" for char in text))
                    published.append(text)
            self.assertEqual(set(codes), {bytes([code]) for code in
                             range(int(new["/FirstChar"]), int(new["/LastChar"]) + 1)})
            self.assertEqual(len(codes), len(set(codes)))
        self.assertCountEqual(published, [item.text for item in captured["emissions"].values()])
        for index in (1, 65534, 65535):
            self.assertEqual(unicodedata.category(pdf_glyphs._temporary_character(index)), "Co")
        for index in (0, 65536):
            with self.assertRaises(TextMappingError):
                pdf_glyphs._temporary_character(index)


    def test_bad_font_identity_accounting_preserves_the_existing_pdf(self):
        path = self.pdf([self.scene.prose("valid output", size=36)])
        previous = path.read_bytes()
        replacement = self.scene.prose("replacement", size=36)
        real_normalize = pdf_glyphs._normalize_font_metadata
        for mode in ("missing", "unemitted", "usage", "private_text"):
            with self.subTest(mode=mode):
                def corrupted(data, emissions, **kwargs):
                    changed = {key: replace(value) for key, value in emissions.items()}
                    marker = next(iter(changed))
                    if mode == "missing":
                        del changed[marker]
                    elif mode == "unemitted":
                        changed[chr(0x10FFFD)] = replace(changed[marker])
                    elif mode == "usage":
                        changed[marker] = replace(changed[marker], uses=changed[marker].uses + 1)
                    else:
                        changed[marker] = replace(changed[marker], text=marker)
                    return real_normalize(data, changed, **kwargs)
                with patch.object(pdf_glyphs, "_normalize_font_metadata", side_effect=corrupted):
                    with self.assertRaises(TextMappingError):
                        write_snapshot(path, paths_from_mobjects([replacement]), [1, 1, 1, 1])
                self.assertEqual(path.read_bytes(), previous)
                self.assertFalse(list(self.folder.glob(".glyph-*.pdf")))


    def test_source_identities_cross_the_cairo_font_subset_boundary(self):
        text = "A" * 260
        prose = self.scene.prose(text, size=6)
        self.assertGreater(ink_rectangle(prose).x0, 0)
        self.assertLess(ink_rectangle(prose).x1, WIDTH)
        audit = {}
        real_normalize = pdf_glyphs._normalize_font_metadata
        def capture(data, emissions, **kwargs):
            return real_normalize(data, emissions, audit=audit)
        with patch.object(pdf_glyphs, "_normalize_font_metadata", side_effect=capture):
            path = self.pdf([prose])
        self.assertEqual(audit["glyph_identities"], 260)
        self.assertEqual(audit["glyph_uses"], 260)
        self.assertGreaterEqual(len(audit["fonts"]), 2)
        self.assertEqual(len({repr(font["source_fonts"]) for font in audit["fonts"]}), 1)
        self.assertEqual(flattened(self.poppler_text(path)), text)
        with fitz.open(path) as doc:
            self.assert_visible_font_text(doc[0])
            self.assertEqual(flattened(doc[0].get_text()), text)
            self.assertGreaterEqual(len(doc[0].get_fonts()), 2)
            self.assertFalse(any(unicodedata.category(char) == "Co" for char in doc[0].get_text()))


    def test_geometry_only_pdf_survives_empty_emission_finalization(self):
        line = Line([-2, -1, 0], [2, 1, 0], color=PALETTES["light"]["accent"], stroke_width=4)
        path = self.pdf([line])
        with fitz.open(path) as doc:
            page = doc[0]
            self.assertEqual(page.get_text().strip(), "")
            self.assertFalse(page.get_fonts())
            self.assertFalse(page.get_image_info())
            self.assertGreaterEqual(len(page.get_drawings()), 2)
            self.assertLess(min(page.get_pixmap().samples), 250)
