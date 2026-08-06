# Playing a Kaleidescape movie by name

Notes from getting this working, in case they save someone else the same
afternoon. Everything here was verified against a Strato E player and a Terra
movie server (KOS 26.1.0). Nothing in this file is copied from Kaleidescape's
documentation — the interesting parts aren't in it.

## The library cannot be enumerated over the control protocol

`GET_CONTENT_LIST`, `GET_MOVIE_LIST`, `GET_LIBRARY_SIZE` and every neighbouring
spelling return **"Invalid request"**. There is no command that lists what you
own.

The movie server's own web UI does have it, so that is where the library comes
from:

```
http://<server>/movies?collection=All
```

Rows carry the handle in an attribute:

```html
<tr class="movie_container" selection_handle="0-S_c449dd9b" ...>
  <td class="movie_title"><a ...>Air Force One</a></td>
  <td class="movie_genre">Action</td>
```

Cast, director and synopsis come from each title's `/details` page. They are not
needed to *play* anything, but without them a search has nothing to match beyond
titles — and "james bond" is not a title, so a franchise search finds nothing.

## PLAY_MOVIE is undocumented

It appears nowhere in the Rev 17 control protocol manual, whose complete command
vocabulary contains no play-by-handle command at all. The wire format:

```
#<serial-without-leading-zero>/<seq>/PLAY_MOVIE:<handle>:<bookmark>[;<opt>=<val>…]::
```

A working minimal command:

```
#70300002028/1/PLAY_MOVIE:26-0.0-S_c449dd9b:26-0.NUL;158-0=1::
```

Send it as a CR-LF terminated line to the **player** on TCP 10000. The movie
server does not accept playback commands, and the player serves no useful HTTP —
so the two machines are addressed differently and swapping them yields a system
that looks configured and plays nothing.

### Three quirks, each of which fails silently

1. **Address by serial with `#`, not the usual `01/`.** `01/0/PLAY_MOVIE:…`
   returns `000` — success — and does nothing, at any zone. Replies echo the
   serial zero-padded.
2. **The trailing `::` is mandatory.** The message carries three fields:
   handle, `bookmark;options`, and an empty third. Drop it and you get
   `011:Invalid number of parameters`.
3. **The options field is mandatory.** `…:26-0.NUL::` returns `000` and does
   nothing; `…:26-0.NUL;158-0=1::` plays. Isolated on a single title with
   everything else held identical. `158-0` / `158-1` select audio and subtitle
   streams; `158-0=1` works as a generic default.

`26-0.NUL` means "from the beginning". A real bookmark handle resumes from a
saved position, but how to *obtain* one is still unknown, so every play here
starts at the top.

The `26-0.` prefix identifies the library, not the player; handles scraped from
the web UI are bare (`0-S_…`) and must be qualified with it. It is detected at
runtime rather than assumed.

## Never trust status `000`

This is the trap that ties the three quirks together: the player returns `000`
for commands it silently ignores, so a malformed-but-accepted command is
indistinguishable from a working one by status code alone.

Confirm playback by **polling `GET_PLAY_STATUS`**, not by waiting for events.
The player only pushes `TITLE_NAME` / `PLAY_STATUS` notifications after
`ENABLE_EVENTS`, so a connection that never sent it will wait forever and then
report a perfectly good play as a failure — which is exactly what happened here
before it was polled instead.

## Two smaller things

**The player sleeps.** A voice request usually arrives while the room is idle
and the player answers `020 Device is in standby` and does nothing. Send
`LEAVE_STANDBY` and retry once; it wakes almost instantly.

**Field values are escaped.** `:` delimits fields, so a colon inside a value
arrives backslashed — a title comes back as `Home Alone 2\: Lost in New York`
and needs unescaping before it is shown to anyone.
