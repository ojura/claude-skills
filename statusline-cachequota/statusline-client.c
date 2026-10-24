/* Claude Code status line client.
 *
 * Millisecond-scale front end for statusline-render.py --daemon: connects
 * to ~/.claude/statusline.sock, forwards stdin (the harness's JSON), and
 * prints the rendered line. If no daemon answers, it spawns one (detached)
 * and retries briefly; if the socket still cannot be reached, it execs the
 * one-shot python renderer so the statusline never goes blank.
 *
 * Build:  cc -O2 -o ~/.claude/statusline-client statusline-client.c
 *         (install.sh does this, and honours CLAUDE_CONFIG_DIR the same way.)
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <errno.h>
#include <limits.h>

static char sock_path[512];
static char script_path[512];

static char *json_string(char *p, const char *value) {
    *p++ = '"';
    static const char hex[] = "0123456789abcdef";
    for (const unsigned char *s = (const unsigned char *)value; *s; s++) {
        if (*s == '"' || *s == '\\') {
            *p++ = '\\';
            *p++ = (char)*s;
        } else if (*s < 0x20) {
            memcpy(p, "\\u00", 4);
            p += 4;
            *p++ = hex[*s >> 4];
            *p++ = hex[*s & 15];
        } else {
            *p++ = (char)*s;
        }
    }
    *p++ = '"';
    return p;
}

static const char *bounded_env(const char *name, size_t maxlen) {
    const char *value = getenv(name);
    return value && strlen(value) <= maxlen ? value : "";
}

static long native_pid(void) {
    const char *raw = bounded_env("CLAUDE_PID", 10);
    if (!*raw)
        return 0;
    for (const char *s = raw; *s; s++)
        if (*s < '0' || *s > '9')
            return 0;
    errno = 0;
    long pid = strtol(raw, NULL, 10);
    return !errno && pid > 0 && pid <= INT_MAX ? pid : 0;
}

/* The daemon serves multiple Claude processes. Every request carries the raw
 * native frame and its process identity; selection is enabled only by the
 * token injected into this invocation's command by the active native mod.
 * Keep the env values as JSON strings so malformed selection JSON cannot
 * corrupt the native input. Python validates activation and session identity. */
static char *with_view(const char *input, size_t *len, int task_metadata) {
    const char *view = bounded_env("CLAUDE_STATUSLINE_VIEW", 8192);
    const char *active = bounded_env("CLAUDE_STATUSLINE_ACTIVE", 256);
    const char *pane = bounded_env("TMUX_PANE", 256);
    long pid = native_pid();
    static const char prefix[] = "{\"statusline_input\":";
    static const char view_key[] = ",\"statusline_view\":";
    static const char active_key[] = ",\"statusline_activation\":";
    static const char process_key[] = ",\"statusline_process\":{";
    static const char task_flag[] = ",\"statusline_task_metadata\":true";
    size_t cap = *len + (strlen(view) + strlen(active) + strlen(pane)) * 6 + 256;
    char *payload = malloc(cap);
    if (!payload)
        return NULL;
    char *p = payload;
    memcpy(p, prefix, sizeof prefix - 1);
    p += sizeof prefix - 1;
    if (*len) {
        memcpy(p, input, *len);
        p += *len;
    } else {
        memcpy(p, "{}", 2);
        p += 2;
    }
    memcpy(p, view_key, sizeof view_key - 1);
    p = json_string(p + sizeof view_key - 1, view);
    memcpy(p, active_key, sizeof active_key - 1);
    p = json_string(p + sizeof active_key - 1, active);
    memcpy(p, process_key, sizeof process_key - 1);
    p += sizeof process_key - 1;
    if (pid)
        p += sprintf(p, "\"pid\":%ld", pid);
    if (*pane) {
        if (pid)
            *p++ = ',';
        memcpy(p, "\"pane\":", 7);
        p = json_string(p + 7, pane);
    }
    *p++ = '}';
    if (task_metadata) {
        memcpy(p, task_flag, sizeof task_flag - 1);
        p += sizeof task_flag - 1;
    }
    *p++ = '}';
    *len = (size_t)(p - payload);
    return payload;
}

static int write_all(int fd, const char *payload, size_t len) {
    for (size_t off = 0; off < len; ) {
        ssize_t n = write(fd, payload + off, len - off);
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0)
            return -1;
        off += (size_t)n;
    }
    return 0;
}

static int try_connect(void) {
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0)
        return -1;
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    size_t path_len = strlen(sock_path);
    if (path_len >= sizeof addr.sun_path) {
        close(fd);
        errno = ENAMETOOLONG;
        return -1;
    }
    memcpy(addr.sun_path, sock_path, path_len + 1);
    if (connect(fd, (struct sockaddr *)&addr, sizeof addr) == 0)
        return fd;
    close(fd);
    return -1;
}

static void spawn_daemon(void) {
    pid_t pid = fork();
    if (pid != 0) {
        /* parent: reap the intermediate child immediately */
        if (pid > 0)
            waitpid(pid, NULL, 0);
        return;
    }
    /* child: detach fully, then become the daemon */
    if (fork() != 0)
        _exit(0);
    setsid();
    int devnull = open("/dev/null", O_RDWR);
    if (devnull >= 0) {
        dup2(devnull, 0);
        dup2(devnull, 1);
        dup2(devnull, 2);
        if (devnull > 2)
            close(devnull);
    }
    execlp("python3", "python3", "-ES", script_path, "--daemon", (char *)NULL);
    _exit(1);
}

int main(int argc, char **argv) {
    int task_metadata = argc == 2 && strcmp(argv[1], "--task-metadata") == 0;
    if (argc > 1 && !task_metadata)
        return 2;
    const char *home = getenv("HOME");
    const char *cfg = getenv("CLAUDE_CONFIG_DIR");
    if (!home)
        home = "";
    if (cfg && *cfg) {
        snprintf(sock_path, sizeof sock_path, "%s/statusline.sock", cfg);
        snprintf(script_path, sizeof script_path, "%s/statusline-render.py", cfg);
    } else {
        snprintf(sock_path, sizeof sock_path, "%s/.claude/statusline.sock", home);
        snprintf(script_path, sizeof script_path, "%s/.claude/statusline-render.py", home);
    }
    /* Read all of stdin first (the payload is a few KB of JSON). */
    static char buf[262144];
    size_t len = 0;
    ssize_t n;
    while (len < sizeof buf
           && (n = read(0, buf + len, sizeof buf - len)) > 0)
        len += (size_t)n;

    char *wrapped = with_view(buf, &len, task_metadata);
    const char *payload = wrapped ? wrapped : buf;

    int fd = try_connect();
    if (fd < 0) {
        spawn_daemon();
        for (int i = 0; i < 40 && fd < 0; i++) { /* up to ~400ms */
            usleep(10000);
            fd = try_connect();
        }
    }
    if (fd < 0) {
        /* Daemonless fallback: hand stdin to the one-shot renderer. The write
         * happens after the fork, with the renderer already draining the pipe,
         * so a payload larger than the pipe buffer cannot deadlock here. */
        int pipefd[2];
        if (pipe(pipefd) != 0)
            return 1;
        pid_t pid = fork();
        if (pid == 0) {
            close(pipefd[1]);
            dup2(pipefd[0], 0);
            close(pipefd[0]);
            execlp("python3", "python3", "-ES", script_path, (char *)NULL);
            _exit(1);
        }
        if (pid < 0)
            return 1;
        close(pipefd[0]);
        write_all(pipefd[1], payload, len);
        close(pipefd[1]);
        free(wrapped);
        int status = 0;
        waitpid(pid, &status, 0);
        return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
    }

    if (write_all(fd, payload, len) < 0) { /* daemon still answers with best effort */ }
    free(wrapped);
    shutdown(fd, SHUT_WR);
    while ((n = read(fd, buf, sizeof buf)) > 0) {
        if (write(1, buf, (size_t)n) < 0)
            break;
    }
    close(fd);
    return 0;
}
