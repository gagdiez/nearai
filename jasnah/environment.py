import json
import os
import subprocess

import openai

DELIMITER = '\n'
CHAT_FILENAME = 'chat.txt'
TERMINAL_FILENAME = 'terminal.txt'


class InferenceRouter(object):

    def __init__(self, config):
        self._config = config
        self._endpoints = {}

    def completions(self, model, messages):
        """Takes a model `provider:model_name` and a list of messages and returns all completions."""
        assert 'models' in self._config and model in self._config['models'], f'Model {model} not found in config.'
        provider_name, model_path = self._config['models'][model].split(':')
        if provider_name not in self._endpoints:
            assert 'providers' in self._config and provider_name in self._config['providers'], f'Provider {provider_name} not found in config.'
            provider_config = self._config['providers'][provider_name]
            self._endpoints[provider_name] = openai.OpenAI(base_url=provider_config['base_url'], api_key=provider_config['api_key'])
        return self._endpoints[provider_name].chat.completions.create(model=model_path, messages=messages)


class Environment(object):

    def __init__(self, path, config):
        self._path = path
        self._done = False
        self._inference = InferenceRouter(config)
        os.makedirs(self._path, exist_ok=True)
        open(os.path.join(self._path, CHAT_FILENAME), 'a').close()

    def add_message(self, role, message):
        with open(os.path.join(self._path, CHAT_FILENAME), 'a') as f:
            f.write(json.dumps({'role': role, 'content': message}) + DELIMITER)

    def list_messages(self):
        with open(os.path.join(self._path, CHAT_FILENAME), 'r') as f:
            return [json.loads(message) for message in f.read().split(DELIMITER) if message]

    def read_file(self, filename):
        if not os.path.exists(os.path.join(self._path, filename)):
            return ''
        with open(os.path.join(self._path, filename), 'r') as f:
            return f.read()
        
    def write_file(self, filename, content):
        with open(os.path.join(self._path, filename), 'w') as f:
            f.write(content)

    def exec_command(self, command):
        """Executes a command in the environment and logs the output."""
        output = subprocess.run(command, shell=True, cwd=self._path)
        with open(os.path.join(self._path, TERMINAL_FILENAME), 'a') as f:
            f.write(json.dumps({'command': command, 'output': output}) + DELIMITER)
        return output

    def completions(self, model, messages):
        """Returns all completions for given messages using the given model."""
        return self._inference.completions(model, messages)

    def completion(self, model, messages):
        """Returns a completion for the given messages using the given model."""
        return self.completions(model, messages).choices[0].message.content

    def is_done(self):
        return self._done

    def mark_done(self):
        self._done = True

    def save(self, registry):
        """Save Environment to Registry."""
        # TODO
        pass

    def load(self, registry):
        """Load Environment from Registry."""
        # TODO
        pass

    def __str__(self):
        return f'Environment({self._path})'