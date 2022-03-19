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
configuration key matrix.mxid (see [config.ini.example](the example
config.ini)). The homeserver also needs to be configured at this
time. You will also need to enter a password here.

On the first connect TeslaBot will create an access token and a device
id and write that to the _state file_. After this point the password
no longer needs to be available in the config file.

## Slack

You need to create a new app for this in the Slack workspace.

TODO: make these instructions a bit more complete

Invite the bot to the room in the configuration.

## Tesla

On the first startup the bot needs given an authorization to the Tesla
API. This can happen by using the cli tool in
https://pypi.org/project/TeslaPy/ to generate `cache.json` for you and
then pointing tesla.credential_store in the configuration to that
file. Alternatively you can visit the URL the bot will tell you (in
the admin room if one is available, otherwise you need to fish it from
the logs), and once you have entered your Tesla credentials, you will
end up to a page with an error. But this is fine, at this point you
just copy the URL from your web browser and send the command

```
!authorize <paste URL here>
```

to the bot admin room and you're done

## Docker setup

```
mkdir tesla-data
curl https://raw.githubusercontent.com/eras/TeslaBot/master/config.ini.example > tesla-data/config.ini
emacs tesla-data/config.ini # edit for your needs

# Use -ti first time to easily see if everything is alright; once it works, replace it with -d
# Also consider --restart always
docker run -ti --name teslabot -d -v $PWD/teslabot-data:/data ghcr.io/eras/teslabot:latest
```
You should not use `latest` but the correct version tag.

## Commands

| command                     | description                                                                                           |
| ---                         | ---                                                                                                   |
| !help                       | Show the list of commands supported                                                                   |
| !climate on [name]          | Sets climate on. Needs vehicle name if you have more than one Tesla.                                  |
| !climate off [name]         | Sets climate off.                                                                                     |
| !info [name]                | Show information about the device, such as about location, climate and charging                       |
| !at 06:00 command           | At 06:00 (not before the current time) issue a command. Can be climate or info, maybe more in future. |
| !atrm 42                    | Cancels a timer                                                                                       |
| !atq                        | Lists timers                                                                                          |
| !set location-detail detail | Defines how precisely the location is displayed. See !help.                                           |
| !set require-! false        | After this commands no longer need the ! prefix to work.                                              |
| !location add/rm/ls         | Manage locations. See !help.                                                                          |
