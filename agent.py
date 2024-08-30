import json
import httpx   # an HTTP client library and dependency of Prefect
from prefect import flow, task
from openai import AzureOpenAI
import pymongo

AZURE_OPENAI_ENDPOINT = "https://.openai.azure.com"
AZURE_OPENAI_API_KEY = "" 
az_client = AzureOpenAI(azure_endpoint=AZURE_OPENAI_ENDPOINT,api_version="2023-07-01-preview",api_key=AZURE_OPENAI_API_KEY)
MDB_URI = ""
DB_NAME = ""
COLLECTION_NAME = "agent_history"

class ConversationHistory:
    def __init__(self, mongo_uri=None):
        # If MongoDB URI is provided, connect to MongoDB
        if mongo_uri:
            self.client = pymongo.MongoClient(mongo_uri)
            self.db = self.client[DB_NAME]
            self.collection = self.db[COLLECTION_NAME]

    def add_to_history(self, history_object):
        """
        Add a new entry to the conversation history.
        """
        # If MongoDB client is available, insert the conversation into MongoDB
        if self.client:
            self.collection.insert_one(history_object)
 
class Tool:
    def __init__(self, name, description, operation):
        self.name = name
        self.description = description
        self.operation = operation
        self.usage_count = 0

    def run(self, input):
        self.usage_count += 1
        return self.operation(input)

@task
def txt_processing(text: str):
    """Process text"""
    # A task can have tools
    tool1 = Tool("UPPER", "Converts text to uppercase", lambda text: text.upper())
    tool2 = Tool("LOWER", "Converts text to lowercase", lambda text: text.lower())
    
    ai_message = az_client.chat.completions.create(
            model="gpt-4o", response_format={ "type": "json_object" },
            messages=[
                {"role": "user", "content": """
        [available tools]
        - Tool("UPPER", "Converts text to uppercase", lambda text: text.upper())
        - Tool("LOWER", "Converts text to lowercase", lambda text: text.lower())
    
        Find the right `TOOL` to solve `INPUT` based on the provided context.
        If no `TOOL` is applicable given the `INPUT`, RETURN AN EMPTY STRING ("").
        [response criteria]
        - JSON Object with the following keys:
            - TOOL: str
            - INPUT_TO_TOOL: object
        - EXAMPLE:
            {
                "TOOL": "UPPER",
                "INPUT_TO_TOOL": {
                    "text": "abc123",
                }
            }
    """+"\nINPUT: "+text+"\n GO!"}])
    ai_message = json.loads(ai_message.choices[0].message.content)
    if ai_message["TOOL"] == "UPPER":
        return tool1.run(ai_message["INPUT_TO_TOOL"]["text"])
    elif ai_message["TOOL"] == "LOWER":
        return tool2.run(ai_message["INPUT_TO_TOOL"]["text"])
    else:
        return "NO_TOOL"
@task(retries=2)
def get_repo_info(repo_owner: str, repo_name: str):
    """Get info about a repo - will retry twice after failing"""
    url = f"https://api.github.com/repos/{repo_owner}/{repo_name}"
    api_response = httpx.get(url)
    api_response.raise_for_status()
    repo_info = api_response.json()
    return repo_info

@task
def get_contributors(repo_info: dict):
    """Get contributors for a repo"""
    contributors_url = repo_info["contributors_url"]
    response = httpx.get(contributors_url)
    response.raise_for_status()
    contributors = response.json()
    return contributors

@flow(log_prints=True)
def log_repo_info(repo_owner: str = "PrefectHQ", repo_name: str = "prefect"):
    """
    Given a GitHub repository, logs the number of stargazers
    and contributors for that repo.
    """
    repo_info = get_repo_info(repo_owner, repo_name)
    print(f"Stars 🌠 : {repo_info['stargazers_count']}")

    contributors = get_contributors(repo_info)
    print(f"Number of contributors 👷: {len(contributors)}")
    return repo_info

class CustomAgent:
    def __init__(self):
        self.memory = ConversationHistory(mongo_uri=MDB_URI)
        self.objective = """
        Find the right `PROCESS` to solve `INPUT` based on the provided context.
        If no `PROCESS` is applicable given the `INPUT`, RETURN AN EMPTY STRING ("").
        [response criteria]
        - JSON Object with the following keys:
            - PROCESS: str
            - INPUT_TO_PROCESS: object
        - EXAMPLE:
            {
                "PROCESS": "log_repo_info",
                "INPUT_TO_PROCESS": {
                    "repo_owner": "PrefectHQ",
                    "repo_name": "prefect"
                }
            }
        """
        self.process_map = {
            "log_repo_info": """
                Given a GitHub repository, logs the number of stargazers
                and contributors for that repo.
                [input object]
                repo_owner: str
                repo_name: str
                """,
            "text_processing": """
                Given a text input, process it and return the processed text.
                [input object]
                text: str
                """
        }
        self.llm = az_client
        self.llm_model = "gpt-4o"
    def run(self, input):
        # Lets build a string that represents the process map
        process_map_str = ""
        for process_name, process_description in self.process_map.items():
            process_map_str += f"Process: {process_name}\nDescription: {process_description}\n\n"
        # Now lets build a string that represents the input
        input_str = f"Input: {input}\n\n"
        # Now lets build a string that represents the objective
        objective_str = f"Objective: {self.objective}\n\n"
        # Finally, lets build the prompt
        prompt = process_map_str + input_str + objective_str
        ai_message = self.llm.chat.completions.create(
            model=self.llm_model, response_format={ "type": "json_object" },
            messages=[
                {"role": "user", "content": prompt}
            ])
        ai_message = json.loads(ai_message.choices[0].message.content)
        if ai_message.get("PROCESS") and ai_message.get("PROCESS") == "log_repo_info":
            input_to_process = ai_message["INPUT_TO_PROCESS"]
            repo_info = log_repo_info(**input_to_process)
            print("Stargazers: ", repo_info["stargazers_count"])
            self.memory.add_to_history({
                "prompt": prompt,
                "response": repo_info,
                "input": input,
                "input_to_process": input_to_process,
                "process": ai_message["PROCESS"]
            })
        if ai_message.get("PROCESS") and ai_message.get("PROCESS") == "text_processing":
            input_to_process = ai_message["INPUT_TO_PROCESS"]
            txt_result = txt_processing(**input_to_process)
            print("txt_result: ", txt_result)
            self.memory.add_to_history({
                "prompt": prompt,
                "response": txt_result,
                "input": input,
                "input_to_process": input_to_process,
                "process": ai_message["PROCESS"]
            })
            return txt_result
        else:
            print("No process found for input: ", input)
            ai_message = self.llm.chat.completions.create(
                model=self.llm_model,
                messages=[
                {"role": "user", "content": input}
            ])
            self.memory.add_to_history({
                "prompt": prompt,
                "response": ai_message.choices[0].message.content,
                "input": input,
                "input_to_process": None,
                "process": None
            })
            print("AI response: ", ai_message.choices[0].message.content)
            return ai_message.choices[0].message.content

if __name__ == "__main__":
    agent = CustomAgent()
    run1 = agent.run("Give me the stars and contributors for ranfysvalle02/vanilla-agents")
    run2 = agent.run("Make the letter `x` uppercase")
    
