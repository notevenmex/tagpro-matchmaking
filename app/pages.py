"""Admin-editable pages. The Rules page is Markdown stored in the database; until an admin saves
it, the site shows DEFAULT_RULES. The front-end renders the Markdown (headings, lists, bold,
italics, links) without allowing raw HTML."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import auth
from .db import SiteText, get_db, now

DEFAULT_RULES = """\
## Joining a pug
Log in with Discord, then register your TagPro name and the position you play: Offense, Defense or Flex if you're happy with either. You can change it any time from the Home page, and captains see it in the draft. You can also add alt accounts and switch between them; only one of your accounts can be in the queue at a time.

## Teams and ratings
- Hit **Find match**. When 8 players are queued, everyone gets 30 seconds to accept. Missing or declining the ready check locks you out of the queue: 5 minutes the first time, then 15 minutes, 1 hour and 24 hours if it keeps happening the same day.
- Leaving a match or not showing up, once both captains report it, costs 25 Elo and a longer lockout, and the match is called off for everyone else.
- If you wait 45 minutes without the queue filling, you're taken out of it. Just queue again.
- Private queues: admins can add you to invite-only queues (like an S Queue). They show on the Home page, and you can wait in them and the general queue at the same time; when one pops you leave the others. Each private queue has its own Elo and Top 100, worked out the same way as the main one. All the lockouts above apply in every queue.
- The two highest-rated players captain. The lower-rated captain picks first, in 1-2-2-1 order (red, blue, blue, red, red, blue). Aim for two offense and two defense.
- Seven maps are drawn at random from the rotation, every map equally likely. Captains take turns banning, blue first, three bans each, until one is left. Then everyone votes on a server and the red captain shares the group link.
- Your rating is your Elo. Everyone starts at 1000.
- A win between evenly matched teams is worth about 25 Elo. Beating a stronger team earns more and losing to a weaker one costs more. Team strength is the average Elo of its players.
- If you're rated below the game's average, wins count for more and losses for less (up to half again). Players rated above the average get a smaller version of the same, so playing in lower-rated games doesn't cost you. If you keep winning games against higher-rated lobbies, your wins count for even more, up to double.
- Momentum: after 2 wins in a row, each further win in the streak counts 10% more (the 3rd win +10%, the 4th +20%, and so on up to +100%). Losing streaks work the same way, but a loss never costs more than 1.5x.
- Every match is zero-sum: the Elo the winners gain adds up to the Elo the losers lose, so ratings don't inflate over a season.
- Your first 10 games of a season are placements: they count for a bit more, and your Elo stays hidden (shown as "New") until they're done. After that you appear on the Top 100.

## Map voting
Rate any map from −5 to 10 on the Maps page to tell the admins what you think of it. Votes don't change which maps come up in the veto.

## Conduct
- Leaving a game in progress counts as a loss.
- Keep chat civil. Mods can mute or remove players.
"""

router = APIRouter()


def rules_out(row: SiteText | None) -> dict:
    if not row:
        return {"markdown": DEFAULT_RULES, "updated_at": None, "is_default": True}
    return {"markdown": row.body, "updated_at": row.updated_at.isoformat(), "is_default": False}


@router.get("/api/rules")
def get_rules(db: Session = Depends(get_db)):
    return rules_out(db.get(SiteText, "rules"))


class RulesIn(BaseModel):
    markdown: str = Field(max_length=20000)


@router.put("/api/rules")
def put_rules(body: RulesIn, admin_id: str = Depends(auth.require_admin), db: Session = Depends(get_db)):
    row = db.get(SiteText, "rules") or SiteText(key="rules")
    row.body, row.updated_by, row.updated_at = body.markdown, admin_id, now()
    db.add(row)
    db.commit()
    return rules_out(row)


# ---------- the "match ready" DM's invite link and rules (match_dm.py) ----------
def _match_dm_out(db: Session) -> dict:
    from . import match_dm
    s = match_dm.settings(db)
    return {**s, "rule_lines": match_dm.rule_lines(s["rules"])}


@router.get("/api/match-dm")
def get_match_dm(admin_id: str = Depends(auth.require_admin), db: Session = Depends(get_db)):
    return _match_dm_out(db)


class MatchDmIn(BaseModel):
    discord_invite: str = Field(max_length=200, pattern=r"^(https://\S+)?$")
    rules: str = Field(max_length=2000)


@router.put("/api/match-dm")
def put_match_dm(body: MatchDmIn, admin_id: str = Depends(auth.require_admin), db: Session = Depends(get_db)):
    for key, value in (("discord_invite", body.discord_invite.strip()), ("match_rules", body.rules)):
        row = db.get(SiteText, key) or SiteText(key=key)
        row.body, row.updated_by, row.updated_at = value, admin_id, now()
        db.add(row)
    db.commit()
    return _match_dm_out(db)
