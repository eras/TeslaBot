import datetime
import json
import time
from typing import List, Tuple, Optional, Any, Callable, Awaitable, TypeVar, Generic
from dataclasses import dataclass
import traceback

from . import scheduler
from . import log
from . import parser as p
from . import commands as c
from .control import Control, CommandContext
from .commands import Invocation
from .state import State, StateElement
from .utils import round_to_next_second

T = TypeVar('T')

logger = log.getLogger(__name__)

@dataclass
class AppTimerInfo:
    id: int
    command: List[str]
    until: Optional[datetime.datetime]

    def json(self) -> Any:
        # don't serialize id, as it will be the key
        j: Any = {"command": self.command}
        if self.until:
            j["until"] = self.until.isoformat()
        return j

    @staticmethod
    def from_json(id: int, json: Any) -> "AppTimerInfo":
        return AppTimerInfo(id=id,
                            command=json["command"],
                            until=datetime.datetime.fromisoformat(json["until"]) if "until" in json else None)

@dataclass
class SchedulerContext:
    info: AppTimerInfo

    def json(self) -> Any:
        return {"info": self.info.json()}

def timer_entry_to_json(entry: scheduler.Entry[SchedulerContext]) -> Any:
    base = entry.context.json()
    if isinstance(entry, scheduler.OneShot):
        base["time"] = entry.time.isoformat()
    elif isinstance(entry, scheduler.Periodic):
        base["next_time"] = entry.next_time.isoformat()
        base["interval_seconds"] = entry.interval.total_seconds()
    else:
        assert False, "Unsupported timer"
    return base

def timer_entry_from_json(id: int, json: Any, callback: Callable[[scheduler.Entry[SchedulerContext]], Awaitable[None]]) -> scheduler.Entry[SchedulerContext]:
    async def indirect_callback() -> None:
        await callback(entry)
    if "interval_seconds" in json:
        entry: scheduler.Entry[SchedulerContext] = \
            scheduler.Periodic(callback=indirect_callback,
                               time=datetime.datetime.fromisoformat(json["next_time"]),
                               interval=datetime.timedelta(seconds=json["interval_seconds"]),
                               context=SchedulerContext(info=AppTimerInfo.from_json(id, json["info"])))
    else:
        entry = scheduler.OneShot(callback=indirect_callback,
                                  time=datetime.datetime.fromisoformat(json["time"]),
                                  context=SchedulerContext(info=AppTimerInfo.from_json(id, json["info"])))
    return entry


def cmd_adjacent(label: str, parser: p.Parser[T]) -> p.Parser[Tuple[str, T]]:
    return p.Labeled(label=label, parser=p.Adjacent(p.CaptureFixedStr(label), parser).base())

CommandWithArgs = List[str]
def valid_schedulable(app_scheduler: "AppScheduler[T]",
                      include_every: bool,
                      include_until: bool) -> p.Parser[CommandWithArgs]:
    cmds = app_scheduler.schedulable_commands[:]
    if include_every:
        cmds.append(cmd_adjacent("every",
                                 valid_schedule_every(app_scheduler,
                                                      include_until=include_until)).any())
    if include_until:
        cmds.append(cmd_adjacent("until",
                                 valid_schedule_until(app_scheduler,
                                                      include_every=include_every)).any())
    return p.CaptureOnly(p.OneOf(*cmds))

ScheduleAtArgs = Tuple[datetime.datetime,
                       CommandWithArgs]
def valid_schedule_at(app_scheduler: "AppScheduler[T]") -> p.Parser[ScheduleAtArgs]:
    return p.Remaining(p.Adjacent(p.TimeOrDateTime(), valid_schedulable(app_scheduler, include_every=True, include_until=True)))

ScheduleEveryArgs = Tuple[Tuple[datetime.timedelta,
                                Optional[datetime.datetime]],
                          CommandWithArgs]
def valid_schedule_every(app_scheduler: "AppScheduler[T]", include_until: bool) -> p.Parser[ScheduleEveryArgs]:
    return p.Remaining(p.Adjacent(p.Adjacent(p.Interval(),
                                             p.Labeled(
                                                 "until",
                                                 p.Optional_(
                                                     p.Conditional(lambda: include_until,
                                                                   p.Keyword("until", p.TimeOrDateTime()))))),
                                  valid_schedulable(app_scheduler, include_every=False, include_until=include_until)))

ScheduleUntilArgs = Tuple[Tuple[datetime.datetime,
                                Optional[datetime.timedelta]],
                          CommandWithArgs]
def valid_schedule_until(app_scheduler: "AppScheduler[T]", include_every: bool) -> p.Parser[ScheduleUntilArgs]:
    return p.Remaining(p.Adjacent(p.Adjacent(p.TimeOrDateTime(),
                                             p.Labeled(
                                                 "every",
                                                 p.Optional_(
                                                     p.Conditional(lambda: include_every,
                                                                   p.Keyword("every", p.Interval()))))),
                                  valid_schedulable(app_scheduler, include_until=False, include_every=include_every)))


class AppSchedulerState(Generic[T], StateElement):
    app_scheduler: "AppScheduler[T]"

    def __init__(self, app_scheduler: "AppScheduler[T]") -> None:
        self.app_scheduler = app_scheduler

    async def save(self, state: State) -> None:
        entries = await self.app_scheduler._scheduler.get_entries()
        if not state.has_section("timers"):
            state["timers"] = {}
        timers = state["timers"]
        timers.clear()
        for entry in entries:
            timers[str(entry.context.info.id)] = json.dumps(timer_entry_to_json(entry))

class AppScheduler(Generic[T]):
    state: State
    schedulable_commands: List[p.Parser[Tuple[str, T]]]
    _scheduler: scheduler.Scheduler[SchedulerContext]
    _scheduler_id: int
    control: Control
    _commands: Optional[c.Commands[CommandContext]]

    def __init__(self,
                 schedulable_commands: List[p.Parser[Tuple[str, T]]],
                 state: State,
                 control: Control) -> None:
        self.schedulable_commands = schedulable_commands
        self._scheduler = scheduler.Scheduler()
        self._scheduler_id = 1
        self.state = state
        self.control = control
        self._commands = None

    def register(self, commands: c.Commands[CommandContext]) -> None:
        # TODO: flow commands from this function to the callbacks (via context?), so that .invoke works
        assert self._commands is None
        self._commands = commands

        commands.register(c.Function("at", "Schedule operation: at 06:00 climate on or at 1h30m every 10m info",
                                     valid_schedule_at(self), self._command_at))
        commands.register(c.Function("every", "Schedule operation: every 10m info",
                                     valid_schedule_every(self, include_until=True), self._command_every))
        commands.register(c.Function("until", "Schedule operation: until 10:00 info",
                                     valid_schedule_until(self, include_every=True), self._command_until))
        commands.register(c.Function("atrm", "Remove a scheduled operation or a running task by its identifier",
                                     p.Remaining(p.Int()), self._command_rm))
        commands.register(c.Function("atq", "List scheduled operations or running tasks",
                                     p.Empty(), self._command_ls))

    async def _load_state(self) -> None:
        if self.state.has_section("timers"):
            for id, timer in self.state["timers"].items():
                self._scheduler_id = max(self._scheduler_id, int(id) + 1)
                entry = timer_entry_from_json(int(id), json.loads(timer), callback=self._activate_timer)
                await self._scheduler.add(entry)

    async def start(self) -> None:
        await self._load_state()
        await self._scheduler.start()
        self.state.add_element(AppSchedulerState(self))

    async def stop(self) -> None:
        await self._scheduler.stop()

    def _next_scheduler_id(self) -> int:
        id = self._scheduler_id
        self._scheduler_id += 1
        return id

    async def _command_ls(self, context: CommandContext, valid: Tuple[()]) -> None:
        entries = await self._scheduler.get_entries()
        if entries:
            result: List[str] = []
            for entry in entries:
                info = entry.context.info
                if isinstance(entry, scheduler.OneShot):
                    result.append(f"{info.id} {entry.time}: {' '.join(info.command)}")
                if isinstance(entry, scheduler.Periodic):
                    until = f" until {info.until}" if info.until else ""
                    result.append(f"{info.id} {entry.next_time}, repeats every {entry.interval}{until}: {' '.join(info.command)}")
            result_lines = "\n".join(result)
            await self.control.send_message(context.to_message_context(), f"Timers:\n{result_lines}")
        else:
            await self.control.send_message(context.to_message_context(), f"No timers set.")

    async def _command_rm(self, context: CommandContext,
                          id: int) -> None:
        def matches(entry: scheduler.Entry[SchedulerContext]) -> bool:
            logger.debug(f"Comparing {entry.context.info.id} vs {id}")
            return entry.context.info.id == id
        async def remove_entry(entries: List[scheduler.Entry[SchedulerContext]]) -> Tuple[List[scheduler.Entry[SchedulerContext]], bool]:
            new_entries = [entry for entry in entries if not matches(entry)]
            logger.debug(f"remove_entry: {entries} -> {new_entries}")
            return new_entries, len(new_entries) != len(entries)
        changed = await self._scheduler.with_entries(remove_entry)
        if changed:
            await self.state.save()
            await self.control.send_message(context.to_message_context(),
                                            f"Removed timer")
        else:
            await self.control.send_message(context.to_message_context(),
                                            f"No timers matched")

    async def _command_every(self, context: CommandContext,
                             args: ScheduleEveryArgs) -> None:
        (interval, until), command = args
        async def callback() -> None:
            await self._activate_timer(entry)
        scheduler_id = self._next_scheduler_id()
        app_timer_info = AppTimerInfo(id=scheduler_id,
                                      command=command,
                                      until=until)
        sched_context = SchedulerContext(info=app_timer_info)
        message = f"Repeat every {interval}"
        entry = scheduler.Periodic(callback,
                                   time=round_to_next_second(datetime.datetime.now()),
                                   interval=interval,
                                   context=sched_context)
        await self._scheduler.add(entry)
        await self.state.save()
        await self.control.send_message(context.to_message_context(),
                                        message)

    async def _command_until(self, context: CommandContext,
                             args: ScheduleUntilArgs) -> None:
        (until, interval), command = args
        async def callback() -> None:
            await self._activate_timer(entry)
        scheduler_id = self._next_scheduler_id()
        app_timer_info = AppTimerInfo(id=scheduler_id,
                                      command=command,
                                      until=until)
        sched_context = SchedulerContext(info=app_timer_info)
        message = f"Until {until}"
        if interval is None:
            interval = datetime.timedelta(minutes=10)
        entry = scheduler.Periodic(callback,
                                   time=round_to_next_second(datetime.datetime.now()),
                                   interval=interval,
                                   context=sched_context)
        await self._scheduler.add(entry)
        await self.state.save()
        await self.control.send_message(context.to_message_context(),
                                        message)

    async def _command_at(self, context: CommandContext,
                          args: ScheduleAtArgs) -> None:
        time, command = args

        async def callback() -> None:
            await self._activate_timer(entry)
        scheduler_id = self._next_scheduler_id()
        app_timer_info = AppTimerInfo(id=scheduler_id,
                                      command=command,
                                      until=None)
        sched_context = SchedulerContext(info=app_timer_info)
        message = f"Scheduled \"{' '.join(command)}\" at {time} (id {scheduler_id})"
        entry: scheduler.Entry[SchedulerContext] = \
            scheduler.OneShot(callback,
                              time=time,
                              context=sched_context)
        await self._scheduler.add(entry)
        await self.state.save()
        await self.control.send_message(context.to_message_context(),
                                        message)

    async def _activate_timer(self, entry: scheduler.Entry[SchedulerContext]) -> None:
        info = entry.context.info
        command = info.command
        logger.info(f"Timer {info.id} activated")
        next_time = entry.when_is_next(time.time())
        if isinstance(entry, scheduler.OneShot) or \
           (info.until is not None \
            and next_time is not None \
            and next_time > info.until.timestamp()):
            await self._scheduler.remove(entry)
        await self.state.save()
        context = CommandContext(admin_room=False, control=self.control)
        await self.control.send_message(context.to_message_context(), f"Timer activated: \"{' '.join(command)}\"")
        assert self._commands
        invocation = c.Invocation(name=command[0], args=command[1:])
        try:
            await self._commands.invoke(context, invocation)
        except Exception as exn:
            logger.error(f"{context.txn} {exn} {traceback.format_exc()}")
            await self.control.send_message(context.to_message_context(),
                                            f"{context.txn} Exception :(")
        await self._command_ls(context, ())
