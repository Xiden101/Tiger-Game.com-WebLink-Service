# Tiger game.com Web Link

A modern replacement for the PC side of the **Tiger game.com Web Link**. Connect your game.com to your computer, see the high scores saved on your cartridge, upload them to the online leaderboards, and download cheats for your games!

It runs as a small local program that opens in your web browser.

## Features

- **High Scores** – reads every score saved on your game.com and shows them by game.
- **Submit Scores** – uploads your scores to the leaderboards at [gamecom.dreampipe.net](https://gamecom.dreampipe.net) using a one-time code from the website.
- **Download Cheat** – downloads cheats for supported titles to your game.com.
- Stays linked until you click **Disconnect**, so you can send several cheats without reconnecting.

## What you need

- A Tiger game.com
- A Tiger game.com **Web Link** cartridge
- Tiger Weblink cable plus a standard DB25 serial-to-USB cable **or** Internet Cable plus a null-modem DB25 serial-to-USB cable (a male-to-male gender changer adapter may be required).
- A computer with [Python](https://www.python.org/downloads/) 3.9 or newer

## Installing

Download this repository, open a command prompt in the folder, and install the two requirements:

```
python -m pip install -r requirements.txt
```

## Running

```
python weblink.py
```

Your browser opens to the program automatically. If it doesn't, go to <http://127.0.0.1:5151/>.

To close the program, click the red **X** next to the **?**, or press **Ctrl+C** in the command prompt.

## Connecting your game.com

1. Connect your Tiger Game.com with the Web Link cable or Internet Cable to your computer.
3. Power on the game.com and launch Web Link.
4. Press Start and wait for *"Linking..."* to appear on screen.
5. Launch the web link python program, select the COM port your cable is connected to from the drip down list, then click **Connect**.

### Uploading scores

1. On the website, go to **Web Link** → **[ CONNECT NOW ]** and click to get a 6-digit code. Keep that page open.
2. In the program, click **Submit Scores**, enter the code, and click **Submit**.
3. The website page switches to your scores by itself. Enter a username and click **[ SUBMIT SCORES ]**.

Codes last 15 minutes and work once. The leaderboard keeps your best score for each game.

### Downloading cheats

Open the **Download Cheat** tab, choose a cheat, and click **Download Cheat**. Only cheats for games saved on your game.com are listed.

More help and answers to common problems: [Web Link Help](https://gamecom.dreampipe.net/weblinkhelp.php)

## Customizing

The files in `data/` control what the program shows. Edit them with any text editor, then restart the program.

**`data/scores.json`** – display names for games, and which ones to leave off the High Scores tab:

```json
[
  { "gameId": "INDY500", "gameName": "Indy 500", "exclude": false }
]
```

`gameId` is the name the cartridge reports. Games not listed here still show, under that name. Scores of zero are never shown.

**`data/cheats.json`** – the cheats available on the Download Cheat tab (one object, or a list of them):

```json
[
  {
    "gameId": "FMEGAMIX",
    "gameName": "Fighters Megamix",
    "description": "Unlock all Characters",
    "command": "--poke \"25=0x0f\""
  }
]
```

`command` sets bytes in that game's saved record: `--poke "OFFSET=VALUE"`, with several pairs separated by commas (for example `"25=0x0f,26=0x01"`). Offsets count from the start of the game's 64-byte record.

## Project layout

```
app.py                  local web server: COM ports, connecting, scores, cheats
requirements.txt
data/
  scores.json           game names and exclusions
  cheats.json           available cheats
static/
  gamecom_link.py       game.com Web Link serial protocol
  index.html            the program's page
  app.js                page behavior
  style.css             page styling
  tiger.png             logo
```

## Notes

This is a fan project and is not affiliated with Tiger Electronics or Hasbro. game.com is a trademark of its respective owner.
