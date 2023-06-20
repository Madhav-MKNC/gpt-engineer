import json
import re
import subprocess

from gpt_engineer.ai import AI
from gpt_engineer.chat_to_files import to_files
from gpt_engineer.db import DBs


def setup_sys_prompt(dbs):
    return dbs.identity["generate"] + "\nUseful to know:\n" + dbs.identity["philosophy"]


def simple_gen(ai: AI, dbs: DBs):
    """Run the AI on the main prompt and save the results"""
    messages = ai.start(
        setup_sys_prompt(dbs),
        dbs.input["main_prompt"],
    )
    to_files(messages[-1]["content"], dbs.workspace)
    return messages


def clarify(ai: AI, dbs: DBs):
    """
    Ask the user if they want to clarify anything and save the results to the workspace
    """
    messages = [ai.fsystem(dbs.identity["qa"])]
    user = dbs.input["main_prompt"]
    while True:
        messages = ai.next(messages, user)

        if messages[-1]["content"].strip().lower().startswith("no"):
            break

        print()
        user = input('(answer in text, or "q" to move on)\n')
        print()

        if not user or user == "q":
            break

        user += (
            "\n\n"
            "Is anything else unclear? If yes, only answer in the form:\n"
            "{remaining unclear areas} remaining questions.\n"
            "{Next question}\n"
            'If everything is sufficiently clear, only answer "no".'
        )

    print()
    return messages

def perform_ai_task(ai: AI, dbs: DBs, messages: List[Dict[str, str]], output_key: str) -> List[Dict[str, str]]:
    """
    Perform an AI task by generating responses based on the given messages.
    Save the output to the workspace using the specified output_key.
    Return the updated messages.
    """
    messages = messages + [ai.fassistant(dbs.workspace[output_key])]
    messages = ai.next(messages, dbs.identity["use_qa"])
    dbs.workspace[output_key] = messages[-1]["content"]
    to_files(dbs.workspace[output_key], dbs.workspace)
    return messages


def gen_spec(ai: AI, dbs: DBs):
    """
    Generate a spec from the main prompt + clarifications and save the results to
    the workspace
    """
    messages = [
        ai.fsystem(f"Instructions: {dbs.input['main_prompt']}"),
    ]
    return perform_ai_task(ai, dbs, messages, "specification")


def respec(ai: AI, dbs: DBs):
    messages = json.loads(dbs.logs[gen_spec.__name__])
    messages += [ai.fsystem(dbs.identity["respec"])]

    messages = ai.next(messages)
    messages = ai.next(
        messages,
        (
            "Based on the conversation so far, please reiterate the specification for "
            "the program. "
            "If there are things that can be improved, please incorporate the "
            "improvements. "
            "If you are satisfied with the specification, just write out the "
            "specification word by word again."
        ),
    )

    dbs.memory["specification"] = messages[-1]["content"]
    return messages


def gen_unit_tests(ai: AI, dbs: DBs):
    """
    Generate unit tests based on the specification, that should work.
    """
    messages = [
        ai.fsystem(f"Instructions: {dbs.input['main_prompt']}"),
        ai.fsystem(f"Specification:\n\n{dbs.memory['specification']}"),
    ]
    return perform_ai_task(ai, dbs, messages, "unit_tests")


def gen_clarified_code(ai: AI, dbs: DBs):
    # get the messages from previous step

    messages = json.loads(dbs.logs[clarify.__name__])

    messages = [
        ai.fsystem(setup_sys_prompt(dbs)),
    ] + messages[1:]
    messages = ai.next(messages, dbs.identity["use_qa"])

    to_files(messages[-1]["content"], dbs.workspace)
    return messages


def gen_code(ai: AI, dbs: DBs):
    # get the messages from previous step
    messages = [
        ai.fsystem(f"Instructions: {dbs.input['main_prompt']}"),
        ai.fsystem(f"Specification:\n\n{dbs.memory['specification']}"),
        ai.fsystem(f"Unit tests:\n\n{dbs.memory['unit_tests']}"),
    ]
    return perform_ai_task(ai, dbs, messages, "code")


def execute_entrypoint(ai, dbs):
    command = dbs.workspace["run.sh"]

    print("Do you want to execute this code?")
    print()
    print(command)
    print()
    print('If yes, press enter. Otherwise, type "no"')
    print()
    if input() != "":
        print("Ok, not executing the code.")
        return []
    print("Executing the code...")
    print()
    subprocess.run("bash run.sh", shell=True, cwd=dbs.workspace.path)
    return []


def gen_entrypoint(ai, dbs):
    messages = ai.start(
        system=(
            "You will get information about a codebase that is currently on disk in "
            "the current folder.\n"
            "From this you will answer with code blocks that includes all the necessary "
            "unix terminal commands to "
            "a) install dependencies "
            "b) run all necessary parts of the codebase (in parallell if necessary).\n"
            "Do not install globally. Do not use sudo.\n"
            "Do not explain the code, just give the commands.\n"
        ),
        user="Information about the codebase:\n\n" + dbs.workspace["all_output.txt"],
    )
    print()

    regex = r"```\S*\n(.+?)```"
    matches = re.finditer(regex, messages[-1]["content"], re.DOTALL)
    dbs.workspace["run.sh"] = "\n".join(match.group(1) for match in matches)
    return messages


def use_feedback(ai: AI, dbs: DBs):
    messages = [
        ai.fsystem(setup_sys_prompt(dbs)),
        ai.fuser(f"Instructions: {dbs.input['main_prompt']}"),
        ai.fassistant(dbs.workspace["all_output.txt"]),
        ai.fsystem(dbs.identity["use_feedback"]),
    ]
    messages = ai.next(messages, dbs.memory["feedback"])
    to_files(messages[-1]["content"], dbs.workspace)
    return messages


def fix_code(ai: AI, dbs: DBs):
    code_ouput = json.loads(dbs.logs[gen_code.__name__])[-1]["content"]
    messages = [
        ai.fsystem(setup_sys_prompt(dbs)),
        ai.fuser(f"Instructions: {dbs.input['main_prompt']}"),
        ai.fuser(code_ouput),
        ai.fsystem(dbs.identity["fix_code"]),
    ]
    messages = ai.next(messages, "Please fix any errors in the code above.")
    to_files(messages[-1]["content"], dbs.workspace)
    return messages


# Different configs of what steps to run
STEPS = {
    "default": [simple_gen, gen_entrypoint, execute_entrypoint],
    "benchmark": [simple_gen, gen_entrypoint],
    "simple": [simple_gen, gen_entrypoint, execute_entrypoint],
    "tdd": [gen_spec, gen_unit_tests, gen_code, gen_entrypoint, execute_entrypoint],
    "tdd+": [
        gen_spec,
        gen_unit_tests,
        gen_code,
        fix_code,
        gen_entrypoint,
        execute_entrypoint,
    ],
    "clarify": [clarify, gen_clarified_code, gen_entrypoint, execute_entrypoint],
    "respec": [
        gen_spec,
        respec,
        gen_unit_tests,
        gen_code,
        gen_entrypoint,
        execute_entrypoint,
    ],
    "execute_only": [execute_entrypoint],
    "use_feedback": [use_feedback],
}

# Future steps that can be added:
# self_reflect_and_improve_files,
# add_tests
# run_tests_and_fix_files,
# improve_based_on_in_file_feedback_comments
