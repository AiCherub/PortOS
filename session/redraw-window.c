#include <X11/Xlib.h>
#include <stdlib.h>
#include <unistd.h>

static int notify_geometry(Display *display, Window window, int size_delta)
{
    XWindowAttributes attributes;
    XEvent event = {0};
    Window child;

    if (!XGetWindowAttributes(display, window, &attributes) ||
        attributes.map_state != IsViewable ||
        attributes.width <= 1 || attributes.height <= 1)
        return 0;

    event.xconfigure.type = ConfigureNotify;
    event.xconfigure.display = display;
    event.xconfigure.event = window;
    event.xconfigure.window = window;
    if (!XTranslateCoordinates(display, window, attributes.root, 0, 0,
                               &event.xconfigure.x, &event.xconfigure.y, &child))
        return 0;
    event.xconfigure.width = attributes.width + size_delta;
    event.xconfigure.height = attributes.height + size_delta;
    event.xconfigure.border_width = attributes.border_width;

    int sent = XSendEvent(display, window, False, StructureNotifyMask, &event);
    XFlush(display);
    return sent;
}

int main(int argc, char **argv)
{
    if (argc != 2)
        return EXIT_FAILURE;
    char *end;
    Window window = strtoul(argv[1], &end, 16);
    if (!window || *end)
        return EXIT_FAILURE;

    Display *display = XOpenDisplay(NULL);
    if (!display)
        return EXIT_FAILURE;

    /* Prompt a new layout without changing the X window or its restore geometry. */
    int sent = notify_geometry(display, window, -1);
    if (sent) {
        usleep(150000);
        sent = notify_geometry(display, window, 0);
    }
    XCloseDisplay(display);
    return sent ? EXIT_SUCCESS : EXIT_FAILURE;
}
