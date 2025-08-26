import openai

YOUR_OPENAI_API_KEY = "" # TODO update Github Secrets

class LightweightChat:
    def __init__(self, api_key=YOUR_OPENAI_API_KEY, model="gpt-3.5-turbo"):
        self.api_key = api_key
        self.model = model

    def explain_error(self, error_text, tool_name=None):
        client = openai.OpenAI(api_key=self.api_key)
        system_prompt = (
            "You are a helpful assistant for Galaxy tool errors. "
            "Explain the error in technical and scientific terms. "
            "Provide a thorough, step-by-step explanation, including possible causes and solutions. "
            "Write in multiple paragraphs if needed."
        )
        user_message = f"Error: {error_text}"
        if tool_name:
            user_message += f"\nTool: {tool_name}"
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message}
                ],
                max_tokens=200,  # Still limits the length, but prompt encourages detail
                temperature=0.7,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            return f"Could not generate explanation: {str(e)}"
