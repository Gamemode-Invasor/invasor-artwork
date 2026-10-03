import { currentGame, defineModule, ui, type Game, type ModuleCtx, type WindowSpec } from "invasor";

// Artwork: browse community artwork (from steamgriddb.com) for the game you're on and apply it.
// Panel: Artwork (target game + "Change artwork…"), Settings (API key, filters), Credits.
// The expanded view has one tab per artwork kind plus Manage (current art, clear it).

type Kind = "capsule" | "wide" | "hero" | "logo";

interface Status {
  has_key: boolean;
  user: string | null;
}

interface Match {
  id: number;
  name: string;
  verified?: boolean;
  /** steam: Steam's own entry · search: first result for its name · chosen: picked by the user */
  source: "steam" | "search" | "chosen";
}

interface Image {
  id: number;
  url: string;
  thumb: string;
  width: number;
  height: number;
  style: string;
  author: string | null;
  animated?: boolean;
}

const KINDS: { id: Kind; label: string; aspect: "grid" | "hero" | "logo" | number; columns?: number }[] = [
  { id: "capsule", label: "Capsule", aspect: "grid" },
  { id: "wide", label: "Wide capsule", aspect: 215 / 460, columns: 3 },
  { id: "hero", label: "Hero", aspect: "hero" },
  { id: "logo", label: "Logo", aspect: "logo" },
];

const errorText = (e: unknown) => String((e as Error)?.message ?? e);

// ---------- panel: Artwork ----------

let artworkEl: HTMLElement | null = null;
let artworkSig = "";

async function renderArtwork(ctx: ModuleCtx, force = false) {
  const el = artworkEl;
  if (!el) return;
  let status: Status | null = null;
  try {
    status = await ctx.call<Status>("status");
  } catch (e) {
    ctx.toast(`Artwork: ${errorText(e)}`, "error");
  }
  const t = currentGame(ctx.game());
  const sig = JSON.stringify([t?.game.appid, t?.how, status]);
  if (!force && sig === artworkSig) return; // nothing changed: keep the ring where it is
  artworkSig = sig;

  const parts: HTMLElement[] = [];
  if (!status?.has_key) {
    parts.push(
      ui.info("Artwork needs your own steamgriddb.com API key (free):"),
      ui.info("steamgriddb.com → log in with Steam → Preferences → API, then paste it in Settings (L2/R2)."),
    );
  }
  if (status && !status.user) parts.push(ui.info("Couldn't find your Steam account's folder (userdata)."));
  if (!t) {
    parts.push(ui.info("Open or highlight a game in the Library."));
  } else {
    parts.push(ui.info(`${t.game.name ?? `App ${t.game.appid}`} (${t.how})`));
    if (status?.has_key && status.user) {
      const game = t.game;
      parts.push(ui.windowButton(ctx, { label: "Change artwork…", open: () => artworkWindow(ctx, game) }));
    } else {
      const locked = ui.button({ label: "Change artwork…", onClick: () => {} });
      locked.setDisabled(true, status?.has_key ? "No Steam account folder" : "Add your API key in Settings");
      parts.push(locked);
    }
  }
  el.replaceChildren(...parts);
}

// ---------- expanded view ----------

function artworkWindow(ctx: ModuleCtx, game: Game): WindowSpec {
  const appid = game.appid;
  let matching: Promise<Match | null> | null = null;
  const reloaders = new Set<() => void>(); // built tabs, reloaded when the match changes
  let refreshManage: (() => void) | null = null;

  const getMatch = () =>
    (matching ??= ctx.call<Match | null>("match", { appid, name: game.name, shortcut: game.shortcut }).catch((e) => {
      ctx.toast(`Artwork: ${errorText(e)}`, "error");
      return null;
    }));
  const rematch = (m: Match | null) => {
    matching = m ? Promise.resolve(m) : null;
    for (const reload of reloaders) reload();
  };

  /** "Change game": search SteamGridDB by name and pick the right entry. */
  function searchUi(body: HTMLElement) {
    const field = ui.text({ label: "Search", value: game.name ?? "", maxLength: 100 });
    const results = document.createElement("div");
    const go = ui.button({
      label: "Search SteamGridDB",
      onClick: async () => {
        results.replaceChildren(ui.info("Searching…"));
        try {
          const found = await ctx.call<Match[]>("search_games", { term: field.get() });
          results.replaceChildren(
            ...(found.length
              ? found.slice(0, 12).map((g) =>
                  ui.button({
                    label: g.verified ? `${g.name} ✓` : g.name,
                    onClick: async () => {
                      try {
                        await ctx.call("set_match", { appid, game_id: g.id, name: g.name });
                        ctx.toast(`Using “${g.name}”`);
                        rematch({ ...g, source: "chosen" });
                      } catch (e) {
                        ctx.toast(errorText(e), "error");
                      }
                    },
                  }),
                )
              : [ui.info("No games found.")]),
          );
        } catch (e) {
          results.replaceChildren(ui.info(errorText(e)));
        }
      },
    });
    const automatic = ui.button({
      label: "Back to automatic match",
      onClick: async () => {
        await ctx.call("clear_match", { appid }).catch(() => {});
        rematch(null);
      },
    });
    body.replaceChildren(field, go, automatic, results);
  }

  function kindTab(kind: (typeof KINDS)[number]) {
    return {
      label: kind.label,
      render(el: HTMLElement) {
        const head = ui.info("Looking for the game on SteamGridDB…");
        const body = document.createElement("div");
        el.append(head, ui.button({ label: "Change game", onClick: () => searchUi(body) }), body);

        let items: Image[] = [];
        let page = 0;
        let applying = false;
        let token = 0; // a newer load makes older answers stale

        const apply = async (id: string) => {
          const img = items.find((i) => String(i.id) === id);
          if (!img || applying) return;
          applying = true;
          ctx.toast(`Applying ${kind.label.toLowerCase()}…`);
          try {
            const args = { appid, kind: kind.id, url: img.url };
            let r = await ctx.call<{ refreshed?: boolean; needs_confirm?: boolean; size_mb?: number }>("apply", args);
            if (r.needs_confirm) {
              const ok = await ui.confirm(
                `This image is ${r.size_mb} MB, much bigger than usual artwork. Download and apply it anyway?`,
                { ok: "Download" },
              );
              if (!ok) return;
              ctx.toast(`Downloading ${r.size_mb} MB…`);
              r = await ctx.call("apply", { ...args, allow_big: true });
            }
            ctx.toast(r.refreshed ? `${kind.label} applied` : `${kind.label} applied: restart Steam to see it`);
            refreshManage?.();
          } catch (e) {
            ctx.toast(`Couldn't apply: ${errorText(e)}`, "error");
          } finally {
            applying = false;
          }
        };

        const draw = (more: boolean) => {
          const parts: HTMLElement[] = [];
          if (!items.length) parts.push(ui.info("No artwork found with the current filters (Settings)."));
          else
            parts.push(
              ui.imageGrid({
                items: items.map((i) => ({ id: String(i.id), src: i.thumb, label: `${i.width}×${i.height}${i.animated ? " · animated" : ""}`, video: i.animated })),
                aspect: kind.aspect,
                columns: kind.columns,
                onActivate: (id) => void apply(id),
                activateLabel: "apply",
              }),
            );
          if (more) parts.push(ui.button({ label: "Load more", onClick: () => void load(page + 1) }));
          body.replaceChildren(...parts);
        };

        async function load(p: number) {
          const mine = ++token;
          const m = await getMatch();
          if (mine !== token) return;
          head.textContent = m
            ? `SteamGridDB match: ${m.name}${m.source === "chosen" ? " (chosen)" : ""}`
            : "Not found on SteamGridDB: use Change game.";
          if (!m && game.shortcut) return void body.replaceChildren();
          if (p === 0) body.replaceChildren(ui.info("Loading…"));
          try {
            const query = m && m.source !== "steam" ? { game_id: m.id } : { appid };
            const res = await ctx.call<Image[]>("assets", { kind: kind.id, page: p, ...query });
            if (mine !== token) return;
            items = p === 0 ? res : [...items, ...res];
            page = p;
            draw(res.length > 0);
          } catch (e) {
            if (mine === token) body.replaceChildren(ui.info(errorText(e)));
          }
        }

        reloaders.add(() => void load(0));
        void load(0);
      },
    };
  }

  const manage = {
    label: "Manage",
    render(el: HTMLElement) {
      const draw = async () => {
        let current: Record<string, string> = {};
        try {
          current = await ctx.call<Record<string, string>>("current", { appid });
        } catch (e) {
          ctx.toast(errorText(e), "error");
        }
        el.replaceChildren(
          ui.info("Custom artwork of this game. Clearing goes back to Steam's own."),
          ...KINDS.map((k) => {
            const file = current[k.id];
            if (!file) return ui.section(k.label, [ui.info("Steam's default")]);
            return ui.section(k.label, [
              ui.image(`https://steamloopback.host/customimages/${file}`, { alt: k.label }),
              ui.button({
                label: `Clear ${k.label.toLowerCase()}`,
                onClick: async () => {
                  if (!(await ui.confirm(`Go back to Steam's ${k.label.toLowerCase()} for this game?`, { ok: "Clear" }))) return;
                  try {
                    const r = await ctx.call<{ refreshed: boolean }>("clear", { appid, kind: k.id });
                    ctx.toast(r.refreshed ? `${k.label} cleared` : `${k.label} cleared: restart Steam to see it`);
                  } catch (e) {
                    ctx.toast(errorText(e), "error");
                  }
                  void draw();
                },
              }),
            ]);
          }),
        );
      };
      refreshManage = () => void draw();
      return draw();
    },
  };

  return {
    title: `Artwork · ${game.name ?? `App ${appid}`}`,
    tabs: [...KINDS.map(kindTab), manage],
  };
}

// ---------- module ----------

export default defineModule({
  tabs: [
    {
      label: "Artwork",
      async render(el, ctx) {
        artworkEl = el;
        artworkSig = "";
        await renderArtwork(ctx, true);
      },
      // The API key may have been added in Settings meanwhile: refresh if anything changed.
      onShow: (ctx) => void renderArtwork(ctx),
    },
    {
      label: "Settings",
      async render(el, ctx) {
        el.append(await ui.settingsForm(ctx));
      },
    },
    {
      label: "Credits",
      render(el) {
        el.append(
          ui.info("Artwork shows community artwork from SteamGridDB (steamgriddb.com) for your games."),
          ui.separator(),
          ui.info("Thanks to the SteamGridDB community and its artists. Please respect their work."),
          ui.info("Inspired by decky-steamgriddb by the SteamGridDB team (GPL-3.0): github.com/SteamGridDB/decky-steamgriddb"),
          ui.separator(),
          ui.info("Artwork is not affiliated with SteamGridDB. Please don't report problems with this module to them."),
        );
      },
    },
  ],
  tabsAlign: "justify",
  onGameChange: (_game, ctx) => void renderArtwork(ctx),
  destroy: () => {
    artworkEl = null;
    artworkSig = "";
  },
});
