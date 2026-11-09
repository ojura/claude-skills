# Data-only assembly on a constrained build host

Prepare the executable runtime locally, then let the hosted document build
append fresh PDFs, vector timelines and catalog metadata as data. The hosted
stage uses installed Python and TeX. It does not start an uploaded executable.
The resulting AppImage uses the same native player, fonts and libraries as its
locally prepared template; the lecturer does not need Python or TeX.

## Prepare the runtime and transport

Build an ordinary package in the toolkit's Ubuntu 20.04 x86_64 container first,
as described in [Toolkit installation and interfaces](toolkit.md#use-native-presentation-tools).
Retain its `build/appimage/work/AppDir` and verified `tools` directory. Inside
that same builder, create a new template directory:

```bash
python3 tools/derivations.py package-template \
  --appdir build/appimage/work/AppDir --tools build/appimage/work/tools
```

The default destination is `build/appimage/template/`. The builder checks the
catalog's exact native repository/revision, the original runtime's GLIBC
baseline, current generic launcher and pinned packer/runtime hashes. It copies
the closure, removes its presentation data, compiles the bounded data reader,
and packages the template. Every retained regular runtime file is compared with
the original. `template.json` records those identities, reader requirements,
policy and launcher hashes, and the complete template digest.

Select the catalog deck to build on the host, or use `all`:

```bash
python3 tools/derivations.py package-prepare example \
  --template build/appimage/template/template.appimage \
  --template-info build/appimage/template/template.json \
  --source-revision "$(git rev-parse HEAD)"
```

The default `build/appimage/transport/` contains regular 16 MB template parts
and `config.json`. The configuration records each part's size/hash, the whole
template identity, selected deck(s), application ID, source provenance and
catalog digest. These files are data. Copy this reviewed transport directory
into the isolated upload source when the host needs tracked inputs; its source
location belongs in the consumer's small opt-in adapter. Keep generated work
under `build/`, with the syncing tool's own `**/build/**` ignore confirmed before
building in a live replica.

Existing template and transport destinations are preserved. A changed launcher,
reader, policy, native pin or catalog requires an updated matching preparation.

## Assemble documents and data

Use the installed entry point on the host:

```bash
python3 -B tools/derivations.py package-assemble \
  --transport build/appimage/transport
```

For every selected deck without explicit compiled inputs, this builds both
light and dark live editions with the installed TeX engine and the catalog's
tracked figure assets. Each deck gets a newly claimed private cache and a copied
source view. Source-relative TeX inputs and compiler diagnostics are retained;
this mode creates no links, copies no extended attributes and uses no shared
cache lock. Ordinary `build` invocations keep their existing locks. The same
opt-in source-build options are available directly:

```bash
python3 tools/derivations.py build example --theme both --mode live \
  --copy-workspace --isolated-cache --cache-root build/new-private-cache
```

`--isolated-cache` requires one deck and an explicit directory that does not
exist. It uses installed dependencies and existing assets; dependency fetching
and Manim regeneration remain separate build steps.

For articles or other externally built documents, pass the ordinary package
input map with `--inputs`. Paths inside that map resolve from the project root:

```json
{
  "article": {"light": {"pdf": "build/report.pdf"}},
  "companion": {
    "light": {"pdf": "build/light.pdf", "vectors": "build/light.vector.json"},
    "dark": {"pdf": "build/dark.pdf", "vectors": "build/dark.vector.json"}
  }
}
```

The catalog writer preserves the actual available themes, language, archive
flag, title, default selection and arbitrary project/deck metadata. An external
document with no compiled input is reported as an error. Its existing build
workflow remains responsible for producing that PDF.

Default artifacts go to `build/appimage/assembled/`:

- `output.appimage`: unchanged template followed by the validated data trailer.
- `output.assets.zip`: the exact catalog data and document inputs.
- `output.assembly.json`: input hashes, source provenance, phase and result.
- `output.assembly-build-info.txt`: runtime/template and document provenance.
- `output.assembly-diagnostics.zip`: TeX logs from the private work directory.

`--output-dir` selects another build/external destination. When the hosting
service only advertises artifacts from its current compile directory, use the
explicit `--host-output-dir .` instead. This option permits that host directory,
including the source working directory; all named existing artifacts and
symlinks are preserved. Exclusive report creation claims an output set, and
the AppImage writer independently publishes without overwriting another result.

An Overleaf consumer adapter can run the command from its opt-in `latexmkrc`
hook, guarded by a source-owned enable flag. It needs only the installed toolkit,
transport path and artifact destination; it should contain no format, runtime,
catalog or extraction implementation. Review a forward restoration commit
before a temporary live experiment, retrieve its advertised outputs, and
restore/recompile the ordinary source afterward.

Retrieve custom artifacts through the host's supported output API. In the
tested Overleaf setup the individual `.appimage` route returned an empty 403,
while the advertised complete compile archive included the exact image. The
supported archive retrieval, exact-member extraction and receipt digest check
provided the delivered bytes. Retrieve those outputs before another compile
changes the latest artifact set.

## Format, runtime and integrity

The complete file is the unchanged AppImage template, contiguous independently
DEFLATE-compressed data streams, a canonical UTF-8 JSON manifest, and a 136-byte
footer. The footer stores the 16-byte `AIPAYLOAD-v1` marker (NUL padded), three
little-endian 64-bit lengths, and SHA256 digests for the template, compressed
payload and manifest. Each manifest file has its relative path, exact offset,
compressed/uncompressed length and uncompressed SHA256 digest.

The trusted policy in the executable template permits only
`usr/share/derivation-presentations/` data: PDFs, JSON, text, TSV and the named
catalog metadata. The reader checks the complete file, path components,
file/directory conflicts, stream termination and all file digests. Limits are
4096 files, a 4 MiB manifest, 1 GiB per decompressed file, 2 GiB total data,
1024-byte paths and 255-byte path components. Payload files are never executed.

Both FUSE and `APPIMAGE_EXTRACT_AND_RUN=1` supply `APPIMAGE`, pointing to the
complete file. Direct use of an extracted AppDir also requires
`APPIMAGE=/absolute/path/to/complete.appimage`. After downloading, grant the
image execute permission on the lecturer's machine and query its actual catalog:

```bash
chmod u+x output.appimage
APPIMAGE_EXTRACT_AND_RUN=1 ./output.appimage --list
APPIMAGE_EXTRACT_AND_RUN=1 ./output.appimage --deck article --print-pdf
```

The bounded reader materializes validated data under
`<cache-root>/payloads/<content-identity>/`, using private directories, a runtime
lock and non-executable regular files. It validates the complete cache on each
reuse. A changed cache entry receives a concrete error; removing that one
content entry reconstructs it from the original image on the next launch.

`--list` and `--print-pdf` use temporary extraction and remove it after the
query. The printed PDF path is the durable application output path that the
subsequent launch will create, rather than the temporary extraction path.
`DERIVATION_CACHE_ROOT`, `DERIVATION_BUILD_DIR`, `--cache-root` and `--output-dir`
retain the ordinary application behavior.

## Required acceptance

From an installed consumer root, run:

```bash
PYTHONPATH="$PWD/tools/vendor/manim_pdfpc" python3 -B -m unittest \
  derivation_toolkit.tests.test_payload_bundle \
  derivation_toolkit.tests.test_data_assembly \
  derivation_toolkit.tests.test_private_build -v
```

Run `tests.test_payload_bundle` against the actual compiled reader. It compiles
one when GLib/JSON-GLib/zlib development files and `cc` are present, or accepts
an explicit `PAYLOAD_READER` path. The tests cover integrity corruption,
truncation, bounds, traversal, duplicate/conflicting paths, escaped NULs,
concurrent publication, cache tampering, transient cleanup, Python/native
inspection agreement and the durable printed-PDF contract.

`tests.test_data_assembly` covers generic metadata, light-only inputs, transport
identity and preservation of existing outputs. `tests.test_private_build`
checks the separate new-cache path and actual TeX compilation when installed
Beamer is available. The assembly case denies subprocess launch, links, locks
and xattr operations while packaging prebuilt inputs.

Before delivery, exercise the exact resulting image offline on Ubuntu 20.04 and
24.04 with its actual input map: available themes, all selected live scenes,
fonts, final states and static document selections. Check the generated
template and final-image hashes against their receipts. If the hosted build
produced the image, use the retrieved hosted bytes for this gate.
