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
- The two highest-rated players captain. The lower-rated captain picks first, in 1-2-2-1 order (red, blue, blue, red, red, blue). Aim for two offense and two defense.
- Five maps are drawn by vote chance. Captains take turns banning, blue first, until one is left. Then everyone votes on a server and the red captain shares the group link.
- Ratings work like FACEIT: everyone starts at 1000 Elo, and your Elo sets your skill level from 1 to 10 (level 10 is 2001+).
- A win between evenly matched teams is worth about 25 Elo. Beating a stronger team earns more and losing to a weaker one costs more. Everyone on a team moves by the same amount.
- Your first 10 games of a season are placements: they count for a bit more, and your Elo and level stay hidden until they're done. After that you appear on the Top 100.

## Map voting
Rate any map from −5 to 10 on the Maps page. Your vote counts until you change it, and the rotation updates as votes come in.

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
