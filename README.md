Copyright Erkki Seppälä <erkki.seppala@vincit.fi> 2022

# TeslaBot

..for [Matrix](https://matrix.org) and
[Slack](https://slack.com). Licensed under the [MIT
license](LICENSE.MIT).

TeslaBot allows interfacing with your Tesla vehicle over Matrix or
Slack. It provides functions such as turning climate control on or
off, determining the location of the vehicle (with a list of
pre-configured locations for labeling location or for limiting
information), and adding timers for those functions.

# Setup

First choose if you want to control the bot via Matrix or Slack. Can't
do both with one bot this time.

## Matrix

Chose Matrix? Good! Then you need to create a new Matrix id (aka mxid)
in the homeserver of your choice. Once you have that, use that as the
configuration key `matrix.mxid` (see [the example config.ini](config.ini.example)).
The homeserver also needs to be configured at this time. You will also
need to enter a password here.

On the first connect TeslaBot will create an access token and a device
id and write that to the _state file_. After this point the password
no longer needs to be available in the config file.

The bot supports end-to-end encryption. To make use of this you need
to add the list of trusted mxids in the configuration. This is not
very secure against active attacks where e.g. an attacker is able to
introduce new devices to the device list, but it should be secure
against passive attacks where an attacker gains access to the
(encrypted) messages.

You also may want to add the bot device as a trusted device in your
Matrix client of choice.

Contributions for a more secure way to do this are welcome, but
ultimately it will be solved once [matrix-nio supports
cross-signing](https://github.com/poljar/matrix-nio/issues/229),
making the normal operation of this function quite a lot less tedious.

## Slack

Controlling your corporate fleet? First you need to create a new app
for this in the Slack workspace.

The bot uses websockets, so no need to configure any inbound hooks,
just any random box will do.

TODO: make these instructions a bit more complete

Invite the bot to the room in the configuration.

Set environment variables: 
  - ENVIRONMENT: if running on google cloud, use gcp
  - CHANNEL: slack channel
  - SLACK_ADMIN_CHANNEL_ID: Channel's id that's used for authentication
  - CONTROL: slack
  - EMAIL: tesla login email
  - STORAGE: type of storage (local / firestore)
  - GCP_PROJECT_ID: speaks for itself
  - SLACK_APP_SECRET_ID: secret id for retrieving slack app key in google secret manager
  - SLACK_API_SECRET_ID: secret id for retrieving slack api key in google secret manager

Firestore requires the bot to be run on gcp, because authentication is done automatically there.

## Tesla

On the first startup the bot needs given an authorization to the Tesla
API. This can happen by using the cli tool in
https://pypi.org/project/TeslaPy/ to generate `cache.json` for you and
then pointing `tesla.credential_store` in the configuration to that
file. Alternatively you can visit the URL the bot will tell you (in
the admin room if one is available, otherwise you need to fish it from
the logs), and once you have entered your Tesla credentials, you will
end up to a page with an error. But this is fine, at this point you
just copy the URL from your web browser and send the command

```
!authorize <paste URL here>
```

to the bot admin room and you're done

## Setup with Docker

```
mkdir tesla-data
curl https://raw.githubusercontent.com/eras/TeslaBot/master/config.ini.example > tesla-data/config.ini
emacs tesla-data/config.ini # edit for your needs

# Use -ti first time to easily see if everything is alright; once it works, replace it with -d
# Also consider --restart always
docker run -ti --name teslabot -d -v $PWD/teslabot-data:/data ghcr.io/eras/teslabot:latest
```
You should not use `latest` but the correct version tag. The image currently weighs around 130MB, so it's not tiny, but not huge either.

You can build the Docker image yourself with:
```
git pull https://github.com/eras/TeslaBot
cd TeslaBot
docker build -t teslabot .
```

and then replace the `ghcr..` in the `docker run` command with `teslabot`.

You can also use `docker-compose up` to build and start in one go. Review [the very basic yaml file](docker-compose.yaml) first.

## Installation without docker

```
# optional:
python3 -m venv teslabot
. teslabot/bin/activate
pip3 install wheel

sudo apt install -y libolm-dev libffi-dev
git clone https://github.com/eras/TeslaBot
pip3 install ./TeslaBot[matrix,slack]
curl https://raw.githubusercontent.com/eras/TeslaBot/master/config.ini.example > config.ini
emacs config.ini # edit for your needs

# Run and do the API authentication
python3 -m teslabot --config config.ini
```

You can use e.g. `screen`, `tmux` or `systemd` to arrange this process to run on the background.

## Commands

| command                                | description                                                                                           |
| ---                                    | ---                                                                                                   |
| !help                                  | Show the list of commands supported                                                                   |
| !climate on [name]                     | Sets climate on. Needs vehicle name if you have more than one Tesla.                                  |
| !climate off [name]                    | Sets climate off.                                                                                     |
| !ac on/off [name]                      | Same as !climate.                                                                                     |
| !sauna on/off [name]                   | Sets max defrost on/off.                                                                              |
| !info [name]                           | Show information about the device, such as about location, climate and charging                       |
| !at 06:00 command                      | At 06:00 (not before the current time) issue a command. Can be climate or info, maybe more in future. |
| !at 600 command                        | Same                                                                                                  |
| !at 10m command                        | Schedule at now + 1 minutes                                                                           |
| !at 1h1m command                       | Schedule at now + 1 hour 1 minute                                                                     |
| !at 06:00 every 10m command            | Schedule at 06:00 and re-do every ten minutes                                                         |
| !at 06:00 every 10m until 30m command  | Schedule at 06:00 and re-do every ten minutes for 30 minutes                                          |
| !at 06:00 every 10m until 7:00 command | Schedule at 06:00 and re-do every ten minutes until 30m                                               |
| !atrm 42                               | Cancels a timer                                                                                       |
| !atq                                   | Lists timers                                                                                          |
| !set location-detail detail            | Defines how precisely the location is displayed. See !help.                                           |
| !set require-! false                   | After this commands no longer need the ! prefix to work.                                              |
| !location add/rm/ls                    | Manage locations. See !help.                                                                          |
