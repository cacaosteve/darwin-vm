/* Native development-image input daemon. No fixed guest addresses or LLDB.
 *
 * 24A5430a Recap evidence: RCPEventDeliveryServicePool deliveryServiceFor-
 * SenderProperties: 0x29b1fc1e8; RCPVirtualHIDService postHIDEvent:
 * 0x29b1fff38; touchscreen properties with display UUID 0x29b208234.
 * Keep that virtual service alive instead of creating a player for each tap.
 * The digitizer constructors are exported by the device IOKit framework.
 * See docs/re/native-input.md for ABI sources and runtime status.
 */
#include <dlfcn.h>
#include <dispatch/dispatch.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <mach/mach_time.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <termios.h>
#include <unistd.h>

typedef void *Obj;
typedef void *Sel;
static Sel (*selector)(const char *);
static Obj (*class_named)(const char *);
static void *send_message;
static void (*release_cf)(const void *);
static void *(*pool_push)(void);
static void (*pool_pop)(void *);
static Obj (*digitizer)(const void *, uint64_t, uint32_t, uint32_t,
    uint32_t, uint32_t, uint32_t, double, double, double, double, double,
    int, int, uint32_t);
static Obj (*finger)(const void *, uint64_t, uint32_t, uint32_t, uint32_t,
    double, double, double, double, double, int, int, uint32_t);
static Obj (*keyboard)(const void *, uint64_t, uint32_t, uint32_t, int, uint32_t);
static void (*set_integer)(Obj, uint32_t, intptr_t);
static void (*append_event)(Obj, Obj, uint32_t);
static Obj service_pool, touch_service, button_service;
static Obj touch_properties, display_uuid;
static void *(*instance_variable)(Obj, const char *);
static Obj (*get_ivar)(Obj, void *);

static void initialization_stage(const char *stage, const char *subject) {
    fprintf(stderr, "DVM_INPUT_STAGE stage=%s subject=%s\n", stage,
            subject ? subject : "-");
}

static Obj msg0(Obj object, const char *name) {
    return ((Obj (*)(Obj, Sel))send_message)(object, selector(name));
}
static Obj msg1(Obj object, const char *name, Obj arg) {
    return ((Obj (*)(Obj, Sel, Obj))send_message)(object, selector(name), arg);
}
static Obj event_service(Obj service) {
    return msg0(service, "eventService");
}
static bool service_ready(Obj service) {
    Obj event = NULL;
    Obj client = NULL;
    initialization_stage("eventService.begin", "input-service");
    if (service) event = event_service(service);
    initialization_stage(event ? "eventService.return-nonnull" :
                         "eventService.return-null", "input-service");
    initialization_stage("serviceClient.begin", "event-service");
    if (event) client = msg0(event, "serviceClient");
    initialization_stage(client ? "serviceClient.return-nonnull" :
                         "serviceClient.return-null", "event-service");
    return client != NULL;
}
static bool post_event(Obj service, Obj event) {
    /* HIDVirtualEventService dispatchEvent: returns BOOL. Recap's wrapper
     * returns void and can silently call a service with no registered client
     * (24A5430a 0x2678a7204..0x2678a7238, NATIVE_INPUT_BOOT2). Report the
     * actual submission result, not merely successful event allocation.
     */
    return ((bool (*)(Obj, Sel, Obj))send_message)(
        event_service(service), selector("dispatchEvent:"), event);
}
static void *symbol(void *handle, const char *name) {
    initialization_stage("dlsym.begin", name);
    void *result = dlsym(handle, name);
    if (!result) { fprintf(stderr, "DVM_INPUT_ERROR symbol=%s\n", name); exit(1); }
    initialization_stage("dlsym.return", name);
    return result;
}
static void *framework(const char *path) {
    initialization_stage("dlopen.begin", path);
    void *handle = dlopen(path, RTLD_NOW | RTLD_GLOBAL);
    if (!handle) { fprintf(stderr, "DVM_INPUT_ERROR dlopen=%s error=%s\n", path, dlerror()); exit(1); }
    initialization_stage("dlopen.return", path);
    return handle;
}
static void initialize(void) {
    initialization_stage("initialize.begin", "native-frameworks");
    void *objc = framework("/usr/lib/libobjc.A.dylib");
    framework("/System/Library/Frameworks/Foundation.framework/Foundation");
    void *cf = framework("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation");
    void *iokit = framework("/System/Library/Frameworks/IOKit.framework/IOKit");
    void *bks = framework("/System/Library/PrivateFrameworks/BackBoardServices.framework/BackBoardServices");
    framework("/System/Library/PrivateFrameworks/Recap.framework/Recap");
    selector = symbol(objc, "sel_registerName");
    class_named = symbol(objc, "objc_getClass");
    send_message = symbol(objc, "objc_msgSend");
    instance_variable = symbol(objc, "class_getInstanceVariable");
    get_ivar = symbol(objc, "object_getIvar");
    pool_push = symbol(objc, "objc_autoreleasePoolPush");
    pool_pop = symbol(objc, "objc_autoreleasePoolPop");
    release_cf = symbol(cf, "CFRelease");
    digitizer = symbol(iokit, "IOHIDEventCreateDigitizerEvent");
    finger = symbol(iokit, "IOHIDEventCreateDigitizerFingerEvent");
    keyboard = symbol(iokit, "IOHIDEventCreateKeyboardEvent");
    set_integer = symbol(iokit, "IOHIDEventSetIntegerValue");
    append_event = symbol(iokit, "IOHIDEventAppendEvent");
    Obj uuid = *(Obj *)symbol(bks, "BKSDisplayUUIDMainKey");
    initialization_stage(uuid ? "display-uuid.nonnull" : "display-uuid.null",
                         "BKSDisplayUUIDMainKey");
    Obj properties_class = class_named("RCPEventSenderProperties");
    Obj pool_class = class_named("RCPEventDeliveryServicePool");
    initialization_stage(properties_class ? "class.nonnull" : "class.null",
                         "RCPEventSenderProperties");
    initialization_stage(pool_class ? "class.nonnull" : "class.null",
                         "RCPEventDeliveryServicePool");
    if (!uuid || !properties_class || !pool_class) {
        fprintf(stderr, "DVM_INPUT_ERROR missing-native-class-or-display\n"); exit(1);
    }
    initialization_stage("service-pool.alloc.begin", "RCPEventDeliveryServicePool");
    Obj pool_alloc = msg0(pool_class, "alloc");
    initialization_stage(pool_alloc ? "service-pool.alloc.nonnull" :
                         "service-pool.alloc.null", "RCPEventDeliveryServicePool");
    initialization_stage("service-pool.init.begin", "RCPEventDeliveryServicePool");
    service_pool = pool_alloc ? msg0(pool_alloc, "init") : NULL;
    initialization_stage(service_pool ? "service-pool.init.nonnull" :
                         "service-pool.init.null", "RCPEventDeliveryServicePool");
    initialization_stage("touch-properties.begin", "touchScreenDigitizerSenderForDisplayUUID:");
    Obj properties = msg1(properties_class, "touchScreenDigitizerSenderForDisplayUUID:", uuid);
    initialization_stage(properties ? "touch-properties.nonnull" :
                         "touch-properties.null", "touchScreenDigitizerSenderForDisplayUUID:");
    initialization_stage("touch-properties.retain.begin", "RCPEventSenderProperties");
    display_uuid = uuid;
    touch_properties = msg0(properties, "retain");
    initialization_stage(touch_properties ? "touch-properties.retain.nonnull" :
                         "touch-properties.retain.null", "RCPEventSenderProperties");
    initialization_stage("touch-service.begin", "deliveryServiceForSenderProperties:");
    touch_service = msg0(msg1(service_pool, "deliveryServiceForSenderProperties:", properties), "retain");
    initialization_stage(touch_service ? "touch-service.nonnull" :
                         "touch-service.null", "deliveryServiceForSenderProperties:");
    initialization_stage("button-properties.begin", "phoneButtonSender");
    Obj button_properties = msg0(properties_class, "phoneButtonSender");
    initialization_stage(button_properties ? "button-properties.nonnull" :
                         "button-properties.null", "phoneButtonSender");
    initialization_stage("button-service.begin", "deliveryServiceForSenderProperties:");
    button_service = msg0(msg1(service_pool, "deliveryServiceForSenderProperties:",
                              button_properties), "retain");
    initialization_stage(button_service ? "button-service.nonnull" :
                         "button-service.null", "deliveryServiceForSenderProperties:");
    bool touch_ready = service_ready(touch_service);
    initialization_stage(touch_ready ? "touch-service.ready" :
                         "touch-service.not-ready", "native-registration");
    bool button_ready = service_ready(button_service);
    initialization_stage(button_ready ? "button-service.ready" :
                         "button-service.not-ready", "native-registration");
    if (!touch_ready || !button_ready) {
        fprintf(stderr, "DVM_INPUT_ERROR native-service-registration touch=%d buttons=%d\n",
                touch_ready, button_ready); exit(1);
    }
    fprintf(stderr, "DVM_INPUT_READY protocol=1 touch=%p buttons=%p\n", touch_service, button_service);
}

struct packet { uint64_t sequence; unsigned down, x, y; char kind; };
static bool parse(const char *line, struct packet *packet) {
    int consumed = 0;
    if (sscanf(line, "DVMINPUT1 %" SCNu64 " %c %u %u %u %n", &packet->sequence,
        &packet->kind, &packet->down, &packet->x, &packet->y, &consumed) != 5 || !consumed) return false;
    if (line[consumed] || packet->down > 1) return false;
    return ((packet->kind == 'T' || packet->kind == 'R') &&
            packet->x <= 32767 && packet->y <= 32767) ||
           (packet->kind == 'H' && packet->x == 0 && packet->y == 0) ||
           (packet->kind == 'P' && !packet->down &&
            packet->x <= 32767 && packet->y <= 32767) ||
           (packet->kind == 'S' && !packet->down && !packet->x && !packet->y);
}

static bool post_touch(const struct packet *packet, bool previous_down) {
    uint64_t timestamp = mach_absolute_time();
    /* Match 24A5430a RCPSyntheticEventStream: default transducer type 2
     * (init at 0x29b1fcb74), constructor options 0x40 for parent and child
     * (0x29b213a50, 0x29b213ca8). Range=1, touch=2, position=4, identity=32.
     * Parent/child coordinates are normalized, as native digitizer reports.
     */
    uint32_t mask = previous_down == !!packet->down ? 4 : 1 | 2 | 4 | 32;
    double x = packet->x / 32767.0, y = packet->y / 32767.0;
    Obj hand = digitizer(NULL, timestamp, 2, 0, 0, mask, 0,
                         x, y, 0, 0, 0, packet->down, packet->down, 0x40);
    Obj child = finger(NULL, timestamp, 1, 1, mask, x, y, 0, 0, 0,
                       packet->down, packet->down, 0x40);
    if (!hand || !child) {
        if (hand) release_cf(hand);
        if (child) release_cf(child);
        return false;
    }
    set_integer(hand, (11u << 16) | 25u, 1); /* display integrated */
    set_integer(hand, 4, 1); /* built in: native Recap 0x29b213b58 */
    append_event(hand, child, 0);
    bool submitted = post_event(touch_service, hand);
    release_cf(child);
    release_cf(hand);
    return submitted;
}
static bool post_home(const struct packet *packet) {
    /* Recap's native button constructor, 24A5430a 0x29b213868..0x29b2138d0. */
    Obj event = keyboard(NULL, mach_absolute_time(), 0x0c, 0x40, packet->down, 0x40);
    if (!event) return false;
    set_integer(event, 4, 1);
    bool submitted = post_event(button_service, event);
    release_cf(event);
    return submitted;
}

/* Diagnostic alternative matching touch_bridge.py's verified Recap calls.
 * R packets collect a gesture, then queue native playback on release. This
 * is deliberately separate from T's immediate HID submission; an R ACK
 * means buffering/queueing, not UI delivery. No debugger or fixed addresses.
 */
struct point { double x, y; };
static Obj recap_stream;
static unsigned recap_points;
static void recap_stage(uint64_t sequence, const char *stage,
                        double value0, double value1, const void *object) {
    fprintf(stderr,
            "DVM_INPUT_RECAP sequence=%" PRIu64 " stage=%s value0=%.9g value1=%.9g object=%p\n",
            sequence, stage, value0, value1, object);
}

static bool play_recap(Obj stream, uint64_t sequence) {
    recap_stage(sequence, "finalize.begin", 0, 0, stream);
    msg0(stream, "_finalizeProcessingEventBuffer");
    void *ivar = instance_variable(class_named("RCPSyntheticEventStream"), "_processingEventBuffer");
    if (!ivar) {
        recap_stage(sequence, "processing-buffer.ivar-missing", 0, 0, stream);
        return false;
    }
    Obj events = get_ivar(stream, ivar);
    recap_stage(sequence, events ? "processing-buffer.nonnull" :
                "processing-buffer.null", 0, 0, events);
    msg1(stream, "setEvents:", events);
    Obj options = msg0(msg0(class_named("RCPPlayerPlaybackOptions"), "alloc"), "init");
    if (!options) {
        recap_stage(sequence, "playback-options.null", 0, 0, stream);
        return false;
    }
    msg1(options, "setDisplayUUIDOverride:", display_uuid);
    recap_stage(sequence, "playback-submit.begin", 0, 0, options);
    ((void (*)(Obj, Sel, Obj, Obj, Obj))send_message)(class_named("RCPInlinePlayer"),
        selector("playEventStream:options:completion:"), stream, options, NULL);
    msg0(options, "release");
    recap_stage(sequence, "playback-submit.returned-void", 0, 0, stream);
    return true; /* queued, not proof of delivery or a completed UI action */
}
static bool post_tap(const struct packet *p) {
    /* Use Recap's dedicated native tap generator, 24A5430a 0x29b20041c:
     * x2=tap count, d0/d1=CGPoint, x3=touch count. Direct dispatchEvent:
     * blocked for 2.501s after down in SETTINGS_BITMAP_NATIVE3, producing a
     * long press even with up queued 150ms later. Host or worker sleeps
     * between synchronous direct posts cannot enforce tap duration.
     */
    Obj stream = msg0(msg0(class_named("RCPSyntheticEventStream"), "alloc"), "init");
    if (!stream) return false;
    msg1(stream, "setSenderProperties:", touch_properties);
    struct point size = ((struct point (*)(Obj, Sel))send_message)(stream, selector("screenSize"));
    bool ok = false;
    if (size.x > 0 && size.x <= 16384 && size.y > 0 && size.y <= 16384) {
        struct point point = {p->x * size.x / 32767., p->y * size.y / 32767.};
        ((void (*)(Obj, Sel, uintptr_t, struct point, uintptr_t))send_message)(
            stream, selector("taps:location:withNumberOfTouches:"), 1, point, 1);
        ok = play_recap(stream, p->sequence);
    }
    msg0(stream, "release");
    return ok;
}
static bool post_recap(const struct packet *p) {
    Obj cls = class_named("RCPSyntheticEventStream");
    if (!recap_stream) {
        if (!p->down) {
            recap_stage(p->sequence, "release-without-stream", 0, 0, NULL);
            return true;
        }
        recap_stream = msg0(msg0(cls, "alloc"), "init");
        if (!recap_stream) {
            recap_stage(p->sequence, "stream-init.null", 0, 0, NULL);
            return false;
        }
        recap_stage(p->sequence, "stream-init.nonnull", 0, 0, recap_stream);
        msg1(recap_stream, "setSenderProperties:", touch_properties);
        recap_points = 0;
    }
    struct point size = ((struct point (*)(Obj, Sel))send_message)(
        recap_stream, selector("screenSize"));
    if (!(size.x > 0 && size.x <= 16384 && size.y > 0 && size.y <= 16384)) {
        recap_stage(p->sequence, "screen-size.invalid", size.x, size.y,
                    recap_stream);
        /* Do not retain a partial stream after a rejected point/release. */
        msg0(recap_stream, "release");
        recap_stream = NULL;
        recap_points = 0;
        return false;
    }
    recap_stage(p->sequence, "screen-size.valid", size.x, size.y,
                recap_stream);
    struct point point = {p->x * size.x / 32767., p->y * size.y / 32767.};
    if (!recap_points) {
        recap_stage(p->sequence, "touch-down", point.x, point.y, recap_stream);
        ((void (*)(Obj, Sel, struct point))send_message)(recap_stream,
            selector("touchDown:"), point);
    } else if (p->down && recap_points < 512) {
        recap_stage(p->sequence, "move", point.x, point.y, recap_stream);
        ((void (*)(Obj, Sel, struct point, double))send_message)(recap_stream,
            selector("moveToPoint:duration:"), point, .02);
    }
    recap_points++;
    if (p->down) return true;
    recap_stage(p->sequence, "lift-up", point.x, point.y, recap_stream);
    ((void (*)(Obj, Sel, struct point))send_message)(recap_stream,
        selector("liftUp:"), point);
    bool ok = play_recap(recap_stream, p->sequence);
    msg0(recap_stream, "release");
    recap_stream = NULL;
    return ok;
}

static bool validate_only;
static void *input_loop(void *unused) {
    (void)unused;
    if (!validate_only) {
        fprintf(stderr, "DVM_INPUT_INITIALIZING pid=%d\n", getpid());
        initialize();
    }
    char line[160];
    bool down = false;
    uint64_t last = 0;
    while (fgets(line, sizeof(line), stdin)) {
        if (!strcmp(line, "\n")) continue; /* transport resynchronization */
        /* A long or incomplete record is discarded through its newline. */
        if (!strchr(line, '\n')) {
            int c; while ((c = getchar()) != '\n' && c != EOF) {}
            fprintf(stderr, "DVM_INPUT_REJECT truncated\n"); continue;
        }
        struct packet p;
        if (!parse(line, &p) || p.sequence <= last) {
            fprintf(stderr, "DVM_INPUT_REJECT malformed-or-old\n"); continue;
        }
        void *pool = validate_only ? NULL : pool_push();
        bool ok = validate_only || p.kind == 'S' || (p.kind == 'H' ? post_home(&p) :
                  p.kind == 'P' ? (!down && post_tap(&p)) :
                  p.kind == 'R' ? post_recap(&p) : post_touch(&p, down));
        if (p.kind == 'T' && ok) down = p.down;
        last = p.sequence;
        if (pool) pool_pop(pool);
        fprintf(stderr, "DVM_INPUT_ACK %" PRIu64 " %d\n", p.sequence, ok);
    }
    /* Ensure a disconnected writer cannot leave a finger held down. */
    if (down && !validate_only) {
        struct packet p = {.kind = 'T'};
        post_touch(&p, true);
    }
    fprintf(stderr, "DVM_INPUT_EOF error=%d errno=%d\n", ferror(stdin), errno);
    exit(0);
}
int main(int argc, char **argv) {
    validate_only = argc == 2 && !strcmp(argv[1], "--validate");
    if (argc != 1 && !validate_only) return 2;
    setvbuf(stderr, NULL, _IONBF, 0);
    if (validate_only) { input_loop(NULL); return 0; }
    /* Two readers of /dev/console split packets byte by byte. Keep this
     * descriptor open for the process lifetime; the kernel releases the lock
     * on exit, including SIGKILL. Do this before changing terminal settings.
     */
    int lock = open("/var/run/dvm-input.lock", O_CREAT | O_RDWR | O_CLOEXEC, 0600);
    if (lock < 0 || flock(lock, LOCK_EX | LOCK_NB)) {
        perror("dvm-input singleton lock"); return 1;
    }
    fprintf(stderr, "DVM_INPUT_START version=6 pid=%d\n", getpid());
    /* Inherited stdin was unusable in the 24A5430a cached launchd service:
     * WARM_INPUT_AUTO1 reached HID READY, then fgets returned EOF with
     * tcgetattr's ENOTTY still in errno. The established manual spawn opens
     * /dev/console explicitly. Own that same input endpoint here, after the
     * singleton lock, rather than depending on inherited launchd stdio.
     * --validate continues to read its caller's pipe above.
     */
    if (!freopen("/dev/console", "r", stdin)) {
        perror("dvm-input open console"); return 1;
    }
    /* Direct children of launchd can inherit ignored/blocked signals. */
    signal(SIGTERM, SIG_DFL);
    signal(SIGINT, SIG_DFL);
    sigset_t signals;
    sigemptyset(&signals);
    if (pthread_sigmask(SIG_SETMASK, &signals, NULL)) return 1;
    struct termios attributes;
    if (tcgetattr(STDIN_FILENO, &attributes) == 0) {
        cfmakeraw(&attributes);
        attributes.c_cc[VMIN] = 1;
        attributes.c_cc[VTIME] = 0;
        if (tcsetattr(STDIN_FILENO, TCSANOW, &attributes)) { perror("tcsetattr"); return 1; }
    }
    pthread_t thread;
    if (pthread_create(&thread, NULL, input_loop, NULL)) return 1;
    dispatch_main();
}
