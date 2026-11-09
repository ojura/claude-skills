/* Read a bounded data trailer without trusting archive paths or host Python. */
#define _GNU_SOURCE
#include <glib.h>
#include <json-glib/json-glib.h>
#include <zlib.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <pwd.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define FOOTER_SIZE 136
#define MAX_MANIFEST (4U * 1024U * 1024U)
#define MAX_FILE (1024ULL * 1024ULL * 1024ULL)
#define MAX_TOTAL (2ULL * MAX_FILE)
#define MAX_FILES 4096
#define BLOCK 65536

static const unsigned char magic[16] = "AIPAYLOAD-v1\0\0\0\0";
static int image_fd = -1;
static char *owned_stage;

typedef struct {
    char *path;
    char *sha256;
    uint64_t offset, compressed_bytes, bytes;
} Entry;

typedef struct {
    uint64_t template_bytes, payload_bytes, manifest_bytes;
    char *manifest, *identity;
    GPtrArray *entries;
    GHashTable *paths;
} Payload;

static void die(const char *format, ...) G_GNUC_NORETURN;
static void die(const char *format, ...) {
    va_list args;
    fputs("AppImage payload: ", stderr);
    va_start(args, format);
    vfprintf(stderr, format, args);
    va_end(args);
    fputc('\n', stderr);
    exit(2);
}

/* Only used on the private directory this process just created. */
static void remove_children(int fd) {
    DIR *dir = fdopendir(dup(fd));
    if (!dir) return;
    struct dirent *entry;
    while ((entry = readdir(dir))) {
        if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, "..")) continue;
        int child = openat(fd, entry->d_name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        if (child >= 0) {
            remove_children(child);
            close(child);
            unlinkat(fd, entry->d_name, AT_REMOVEDIR);
        } else {
            unlinkat(fd, entry->d_name, 0);
        }
    }
    closedir(dir);
}

static void cleanup(void) {
    if (image_fd >= 0) close(image_fd);
    if (owned_stage) {
        int fd = open(owned_stage, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        if (fd >= 0) {
            remove_children(fd);
            close(fd);
            rmdir(owned_stage);
        }
    }
}

static void read_exact(int fd, unsigned char *buffer, size_t count, uint64_t offset) {
    while (count) {
        ssize_t got = pread(fd, buffer, count, (off_t)offset);
        if (got < 0 && errno == EINTR) continue;
        if (got <= 0) die("cannot read the complete input");
        count -= (size_t)got;
        buffer += got;
        offset += (uint64_t)got;
    }
}

static void write_exact(int fd, const unsigned char *buffer, size_t count) {
    while (count) {
        ssize_t wrote = write(fd, buffer, count);
        if (wrote < 0 && errno == EINTR) continue;
        if (wrote <= 0) die("cannot write a payload file: %s", strerror(errno));
        count -= (size_t)wrote;
        buffer += wrote;
    }
}

static uint64_t little64(const unsigned char *value) {
    uint64_t result = 0;
    for (unsigned i = 0; i < 8; i++) result |= (uint64_t)value[i] << (8 * i);
    return result;
}

static char *hash_region(int fd, uint64_t offset, uint64_t bytes) {
    GChecksum *sum = g_checksum_new(G_CHECKSUM_SHA256);
    unsigned char buffer[BLOCK];
    while (bytes) {
        size_t take = bytes < BLOCK ? (size_t)bytes : BLOCK;
        read_exact(fd, buffer, take, offset);
        g_checksum_update(sum, buffer, take);
        offset += take;
        bytes -= take;
    }
    char *result = g_strdup(g_checksum_get_string(sum));
    g_checksum_free(sum);
    return result;
}

static char *hex_digest(const unsigned char *bytes) {
    char *result = g_malloc(65);
    for (unsigned i = 0; i < 32; i++) sprintf(result + i * 2, "%02x", bytes[i]);
    return result;
}

static void check_region(uint64_t offset, uint64_t bytes, const unsigned char *expected) {
    char *actual = hash_region(image_fd, offset, bytes);
    char *wanted = hex_digest(expected);
    if (strcmp(actual, wanted)) die("template, data or manifest digest mismatch");
    g_free(actual);
    g_free(wanted);
}

static JsonNode *member(JsonObject *object, const char *name, GType type) {
    JsonNode *node = json_object_get_member(object, name);
    if (!node || !JSON_NODE_HOLDS_VALUE(node) || json_node_get_value_type(node) != type)
        die("invalid manifest member: %s", name);
    return node;
}

static const char *string_member(JsonObject *object, const char *name) {
    return json_node_get_string(member(object, name, G_TYPE_STRING));
}

static uint64_t number_member(JsonObject *object, const char *name) {
    gint64 value = json_node_get_int(member(object, name, G_TYPE_INT64));
    if (value < 0) die("negative manifest length: %s", name);
    return (uint64_t)value;
}

static JsonArray *array_member(JsonObject *object, const char *name) {
    JsonNode *node = json_object_get_member(object, name);
    if (!node || !JSON_NODE_HOLDS_ARRAY(node)) die("missing array: %s", name);
    return json_node_get_array(node);
}

static JsonObject *parse_object(JsonParser *parser, const char *data, size_t size) {
    /* JSON-GLib's C strings lose the suffix after an escaped NUL. Reject that
     * spelling before parsing, while preserving a literal escaped backslash. */
    for (size_t i = 0; i < size; i++) {
        if (data[i] != '\\') continue;
        if (i + 5 < size && data[i + 1] == 'u' && !memcmp(data + i + 2, "0000", 4))
            die("escaped NUL in JSON string");
        i++;
    }
    GError *error = NULL;
    if (!json_parser_load_from_data(parser, data, (gssize)size, &error))
        die("invalid JSON: %s", error ? error->message : "parse failed");
    JsonNode *root = json_parser_get_root(parser);
    if (!root || !JSON_NODE_HOLDS_OBJECT(root)) die("JSON root must be an object");
    return json_node_get_object(root);
}

static int string_array_matches(JsonArray *array, const char *value, int prefix) {
    for (guint i = 0; i < json_array_get_length(array); i++) {
        JsonNode *node = json_array_get_element(array, i);
        if (!node || !JSON_NODE_HOLDS_VALUE(node) || json_node_get_value_type(node) != G_TYPE_STRING)
            die("invalid trusted data policy");
        const char *choice = json_node_get_string(node);
        if (!*choice) die("empty trusted policy rule");
        if (prefix ? g_str_has_prefix(value, choice) : !strcmp(value, choice)) return 1;
    }
    return 0;
}

static void validate_path(const char *path, JsonObject *policy) {
    if (!*path || strlen(path) > 1024 || path[0] == '/' || !g_utf8_validate(path, -1, NULL))
        die("unsafe payload path");
    for (const unsigned char *p = (const unsigned char *)path; *p; p++)
        if (*p < 32 || *p == 127 || *p == '\\') die("unsafe payload path");
    gchar **pieces = g_strsplit(path, "/", -1);
    for (unsigned i = 0; pieces[i]; i++)
        if (!*pieces[i] || !strcmp(pieces[i], ".") || !strcmp(pieces[i], "..") || strlen(pieces[i]) > 255)
            die("unsafe payload path component");
    g_strfreev(pieces);
    if (!string_array_matches(array_member(policy, "prefixes"), path, 1))
        die("payload path is outside the trusted data policy");
    const char *base = strrchr(path, '/');
    base = base ? base + 1 : path;
    const char *suffix = strrchr(base, '.');
    if (!string_array_matches(array_member(policy, "names"), base, 0) &&
        (!suffix || !string_array_matches(array_member(policy, "extensions"), suffix, 0)))
        die("payload file type is outside the trusted data policy");
}

static Payload read_payload(const char *image, JsonObject *policy) {
    Payload result = {0};
    image_fd = open(image, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    struct stat st;
    if (image_fd < 0 || fstat(image_fd, &st) || !S_ISREG(st.st_mode) || st.st_size < FOOTER_SIZE)
        die("the complete AppImage must be a readable regular file");
    uint64_t size = (uint64_t)st.st_size;
    unsigned char footer[FOOTER_SIZE];
    read_exact(image_fd, footer, sizeof footer, size - sizeof footer);
    if (memcmp(footer, magic, sizeof magic)) die("missing data payload footer");
    result.template_bytes = little64(footer + 16);
    result.payload_bytes = little64(footer + 24);
    result.manifest_bytes = little64(footer + 32);
    uint64_t remaining = size - FOOTER_SIZE;
    if (result.template_bytes < 4 || result.template_bytes > remaining) die("invalid template length");
    remaining -= result.template_bytes;
    if (result.payload_bytes > remaining || result.payload_bytes > MAX_TOTAL + MAX_FILES * 1024ULL)
        die("invalid compressed data length");
    remaining -= result.payload_bytes;
    if (result.manifest_bytes != remaining || !remaining || remaining > MAX_MANIFEST)
        die("invalid manifest length");
    unsigned char elf[4];
    read_exact(image_fd, elf, 4, 0);
    if (memcmp(elf, "\177ELF", 4)) die("the template is not an ELF file");
    check_region(0, result.template_bytes, footer + 40);
    check_region(result.template_bytes, result.payload_bytes, footer + 72);
    check_region(result.template_bytes + result.payload_bytes, remaining, footer + 104);
    result.identity = g_compute_checksum_for_data(G_CHECKSUM_SHA256, footer, sizeof footer);
    result.manifest = g_malloc((size_t)remaining + 1);
    read_exact(image_fd, (unsigned char *)result.manifest, (size_t)remaining,
               result.template_bytes + result.payload_bytes);
    result.manifest[remaining] = 0;
    if (strlen(result.manifest) != remaining) die("embedded NUL in manifest");
    char *actual_manifest = g_compute_checksum_for_data(G_CHECKSUM_SHA256,
        (unsigned char *)result.manifest, (size_t)remaining);
    char *wanted_manifest = hex_digest(footer + 104);
    if (strcmp(actual_manifest, wanted_manifest)) die("manifest changed while reading");
    g_free(actual_manifest);
    g_free(wanted_manifest);
    JsonParser *parser = json_parser_new();
    JsonObject *root = parse_object(parser, result.manifest, (size_t)remaining);
    if (strcmp(string_member(root, "format"), "appimage-data-v1")) die("unsupported payload format");
    JsonArray *files = array_member(root, "files");
    guint count = json_array_get_length(files);
    if (!count || count > MAX_FILES) die("invalid payload file count");
    result.entries = g_ptr_array_new();
    result.paths = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
    g_hash_table_insert(result.paths, g_strdup(".payload-manifest.json"), GINT_TO_POINTER(2));
    uint64_t next_offset = 0, total = 0;
    for (guint i = 0; i < count; i++) {
        JsonNode *node = json_array_get_element(files, i);
        if (!JSON_NODE_HOLDS_OBJECT(node)) die("invalid file entry");
        JsonObject *object = json_node_get_object(node);
        Entry *entry = g_new0(Entry, 1);
        entry->path = g_strdup(string_member(object, "path"));
        validate_path(entry->path, policy);
        if (g_hash_table_contains(result.paths, entry->path)) die("duplicate file or directory/file conflict");
        g_hash_table_insert(result.paths, g_strdup(entry->path), GINT_TO_POINTER(2));
        char *parent = g_strdup(entry->path);
        char *slash;
        while ((slash = strrchr(parent, '/'))) {
            *slash = 0;
            if (GPOINTER_TO_INT(g_hash_table_lookup(result.paths, parent)) == 2)
                die("file is also a parent directory");
            g_hash_table_insert(result.paths, g_strdup(parent), GINT_TO_POINTER(1));
        }
        g_free(parent);
        entry->offset = number_member(object, "offset");
        entry->compressed_bytes = number_member(object, "compressed_bytes");
        entry->bytes = number_member(object, "bytes");
        entry->sha256 = g_strdup(string_member(object, "sha256"));
        if (strlen(entry->sha256) != 64) die("invalid file digest");
        for (const char *p = entry->sha256; *p; p++)
            if (!g_ascii_isdigit(*p) && !(*p >= 'a' && *p <= 'f')) die("invalid file digest");
        if (entry->offset != next_offset || !entry->compressed_bytes ||
            entry->compressed_bytes > result.payload_bytes - next_offset ||
            entry->bytes > MAX_FILE || entry->bytes > MAX_TOTAL - total)
            die("file data is outside the declared bounds");
        next_offset += entry->compressed_bytes;
        total += entry->bytes;
        g_ptr_array_add(result.entries, entry);
    }
    if (next_offset != result.payload_bytes) die("unclaimed trailing payload bytes");
    g_object_unref(parser);
    return result;
}

static int file_at(int root, const char *path, int create) {
    gchar **pieces = g_strsplit(path, "/", -1);
    unsigned last = g_strv_length(pieces) - 1;
    int fd = dup(root);
    for (unsigned i = 0; i < last; i++) {
        if (create && mkdirat(fd, pieces[i], 0700) && errno != EEXIST)
            die("cannot create data directory");
        int child = openat(fd, pieces[i], O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        close(fd);
        if (child < 0) die("unsafe or missing data directory");
        fd = child;
    }
    int flags = O_CLOEXEC | O_NOFOLLOW | (create ? O_WRONLY | O_CREAT | O_EXCL : O_RDONLY);
    int result = openat(fd, pieces[last], flags, 0600);
    close(fd);
    g_strfreev(pieces);
    if (result < 0) die("unsafe or missing payload file: %s", path);
    struct stat st;
    if (fstat(result, &st) || !S_ISREG(st.st_mode) || st.st_uid != geteuid() || st.st_nlink != 1)
        die("payload file is not privately owned and regular");
    return result;
}

static void check_tree(int fd, const char *prefix, GHashTable *paths) {
    DIR *dir = fdopendir(dup(fd));
    if (!dir) die("cannot inspect payload cache");
    struct dirent *item;
    while ((item = readdir(dir))) {
        if (!strcmp(item->d_name, ".") || !strcmp(item->d_name, "..")) continue;
        char *path = *prefix ? g_strconcat(prefix, "/", item->d_name, NULL) : g_strdup(item->d_name);
        struct stat st;
        int type = GPOINTER_TO_INT(g_hash_table_lookup(paths, path));
        if (fstatat(fd, item->d_name, &st, AT_SYMLINK_NOFOLLOW) || !type || st.st_uid != geteuid())
            die("unexpected entry in the private payload cache");
        if (type == 1) {
            if (!S_ISDIR(st.st_mode) || (st.st_mode & 0022)) die("unsafe cache directory");
            int child = openat(fd, item->d_name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
            if (child < 0) die("cannot inspect cache directory");
            check_tree(child, path, paths);
            close(child);
        } else if (!S_ISREG(st.st_mode) || st.st_nlink != 1 || (st.st_mode & 0133)) {
            die("cache data must be regular, non-executable files");
        }
        g_free(path);
    }
    closedir(dir);
}

static void verify_cache(int fd, Payload *payload) {
    check_tree(fd, "", payload->paths);
    int marker = file_at(fd, ".payload-manifest.json", 0);
    struct stat st;
    if (fstat(marker, &st) || (uint64_t)st.st_size != payload->manifest_bytes)
        die("cached manifest has changed");
    char *encoded = g_malloc((size_t)payload->manifest_bytes);
    read_exact(marker, (unsigned char *)encoded, (size_t)payload->manifest_bytes, 0);
    close(marker);
    if (memcmp(encoded, payload->manifest, (size_t)payload->manifest_bytes)) die("cached manifest has changed");
    g_free(encoded);
    for (guint i = 0; i < payload->entries->len; i++) {
        Entry *entry = g_ptr_array_index(payload->entries, i);
        int file = file_at(fd, entry->path, 0);
        if (fstat(file, &st) || (uint64_t)st.st_size != entry->bytes) die("cached data length has changed");
        char *actual = hash_region(file, 0, entry->bytes);
        close(file);
        if (strcmp(actual, entry->sha256)) die("cached data digest has changed");
        g_free(actual);
    }
}

static void extract_files(int fd, Payload *payload) {
    unsigned char input[BLOCK], output[BLOCK];
    for (guint i = 0; i < payload->entries->len; i++) {
        Entry *entry = g_ptr_array_index(payload->entries, i);
        int file = file_at(fd, entry->path, 1);
        GChecksum *sum = g_checksum_new(G_CHECKSUM_SHA256);
        z_stream stream = {0};
        if (inflateInit(&stream) != Z_OK) die("cannot initialize the data decompressor");
        uint64_t consumed = 0, written = 0;
        int code = Z_OK;
        while (code != Z_STREAM_END) {
            if (!stream.avail_in && consumed < entry->compressed_bytes) {
                size_t count = entry->compressed_bytes - consumed < BLOCK ?
                    (size_t)(entry->compressed_bytes - consumed) : BLOCK;
                read_exact(image_fd, input, count, payload->template_bytes + entry->offset + consumed);
                consumed += count;
                stream.next_in = input;
                stream.avail_in = (uInt)count;
            }
            stream.next_out = output;
            stream.avail_out = BLOCK;
            code = inflate(&stream, Z_NO_FLUSH);
            size_t count = BLOCK - stream.avail_out;
            if (count > entry->bytes - written) die("decompressed data exceeds its declared bound");
            written += count;
            write_exact(file, output, count);
            g_checksum_update(sum, output, count);
            if (code != Z_OK && code != Z_STREAM_END) die("invalid compressed data stream");
            if (code != Z_STREAM_END && !count && !stream.avail_in && consumed == entry->compressed_bytes)
                die("truncated compressed data stream");
        }
        if (consumed != entry->compressed_bytes || stream.avail_in || written != entry->bytes)
            die("compressed data lengths do not match the manifest");
        inflateEnd(&stream);
        if (strcmp(g_checksum_get_string(sum), entry->sha256)) die("decompressed file digest mismatch");
        g_checksum_free(sum);
        if (fchmod(file, 0444) || fsync(file) || close(file)) die("cannot finish a payload data file");
    }
    int marker = file_at(fd, ".payload-manifest.json", 1);
    write_exact(marker, (unsigned char *)payload->manifest, (size_t)payload->manifest_bytes);
    if (fchmod(marker, 0444) || fsync(marker) || close(marker)) die("cannot finish the cached manifest");
    verify_cache(fd, payload);
}

static char *absolute_cache(const char *given) {
    char *expanded = NULL;
    if (*given == '~') {
        const char *slash = strchr(given, '/');
        size_t length = slash ? (size_t)(slash - given - 1) : strlen(given + 1);
        char *user = g_strndup(given + 1, length);
        struct passwd *record = length ? getpwnam(user) : getpwuid(geteuid());
        if (!record) die("cannot resolve cache home directory");
        expanded = g_strconcat(record->pw_dir, slash ? slash : "", NULL);
        g_free(user);
    }
    char *result = g_canonicalize_filename(expanded ? expanded : given, NULL);
    g_free(expanded);
    return result;
}

static char *materialize(Payload *payload, const char *cache, int transient) {
    if (transient) {
        owned_stage = g_build_filename(g_get_tmp_dir(), "appimage-payload-XXXXXX", NULL);
        if (!g_mkdtemp_full(owned_stage, 0700)) die("cannot create a temporary payload directory");
        int fd = open(owned_stage, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        if (fd < 0) die("cannot open the temporary payload directory");
        extract_files(fd, payload);
        close(fd);
        return g_strdup(owned_stage);
    }
    char *absolute = absolute_cache(cache);
    char *parent = g_build_filename(absolute, "payloads", NULL);
    g_free(absolute);
    if (g_mkdir_with_parents(parent, 0700)) die("cannot create the payload cache");
    int root = open(parent, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    struct stat st;
    if (root < 0 || fstat(root, &st) || st.st_uid != geteuid() || (st.st_mode & 0022))
        die("payload cache must be privately owned and not writable by other users");
    int lock = openat(root, ".lock", O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (lock < 0 || fstat(lock, &st) || !S_ISREG(st.st_mode) || st.st_uid != geteuid() || st.st_nlink != 1)
        die("unsafe cache lock");
    if (flock(lock, LOCK_EX)) die("cannot lock the payload cache");
    char *destination = g_build_filename(parent, payload->identity, NULL);
    if (!fstatat(root, payload->identity, &st, AT_SYMLINK_NOFOLLOW)) {
        if (!S_ISDIR(st.st_mode) || st.st_uid != geteuid() || (st.st_mode & 0022)) die("unsafe payload cache entry");
        int fd = openat(root, payload->identity, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        if (fd < 0) die("cannot open the payload cache entry");
        verify_cache(fd, payload);
        close(fd);
    } else {
        if (errno != ENOENT) die("cannot inspect the payload cache entry");
        owned_stage = g_build_filename(parent, ".payload-stage-XXXXXX", NULL);
        if (!g_mkdtemp_full(owned_stage, 0700)) die("cannot stage the payload cache");
        int fd = open(owned_stage, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        if (fd < 0) die("cannot open the staged payload");
        extract_files(fd, payload);
        close(fd);
        if (rename(owned_stage, destination)) die("cannot publish the validated payload cache");
        g_free(owned_stage);
        owned_stage = NULL;
    }
    close(lock);
    close(root);
    g_free(parent);
    return destination;
}

int main(int argc, char **argv) {
    atexit(cleanup);
    if (argc < 4) die("usage: reader POLICY IMAGE inspect|extract CACHE|run CACHE TRANSIENT PROGRAM [ARGS]");
    gchar *policy_text = NULL;
    gsize policy_size = 0;
    if (!g_file_get_contents(argv[1], &policy_text, &policy_size, NULL) || policy_size > MAX_MANIFEST)
        die("cannot read the template data policy");
    JsonParser *policy_parser = json_parser_new();
    JsonObject *policy = parse_object(policy_parser, policy_text, policy_size);
    if (number_member(policy, "version") != 1) die("unsupported data policy");
    Payload payload = read_payload(argv[2], policy);
    if (!strcmp(argv[3], "inspect")) {
        if (argc != 4) die("inspect takes no further arguments");
        fputs(payload.manifest, stdout);
        return 0;
    }
    int run = !strcmp(argv[3], "run");
    if ((!run && (strcmp(argv[3], "extract") || argc != 5)) || (run && argc < 7))
        die("invalid payload reader arguments");
    int transient = run && !strcmp(argv[5], "1");
    if (run && strcmp(argv[5], "0") && strcmp(argv[5], "1")) die("invalid transient flag");
    char *directory = materialize(&payload, argv[4], transient);
    if (!run) {
        puts(directory);
        return 0;
    }
    if (setenv("APPIMAGE_PAYLOAD_DIR", directory, 1)) die("cannot set the payload directory");
    close(image_fd);
    image_fd = -1;
    if (!transient) {
        execv(argv[6], argv + 6);
        die("cannot launch the bundled application: %s", strerror(errno));
    }
    pid_t child = fork();
    if (child < 0) die("cannot launch the query process");
    if (!child) {
        execv(argv[6], argv + 6);
        _exit(127);
    }
    int status;
    while (waitpid(child, &status, 0) < 0) if (errno != EINTR) die("cannot await the query process");
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
}
