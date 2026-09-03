$tools = @(
    @{
        type = "function"
        function = @{
            name = "read_file"
            description = "Reads the contents of a file"
            parameters = @{
                type = "object"
                properties = @{
                    path = @{
                        type = "string"
                        description = "Path of the file to read"
                    }
                }
                required = @("path")
            }
        }
    }
)

$body = @{
    model = "gpt-4.1"
    messages = @(
        @{
            role = "system"
            content = "You are a coding agent. Use tools whenever they are useful."
        }
        @{
            role = "user"
            content = "Please inspect the file package.json"
        }
    )
    tools = $tools
    tool_choice = "auto"
} | ConvertTo-Json -Depth 20

$response = Invoke-RestMethod `
  -Uri "https://neuron.noser.com/v1/chat/completions" `
  -Headers @{
      Authorization = "Bearer $env:NEURON_API_KEY"
      "Content-Type" = "application/json"
  } `
  -Method Post `
  -Body $body

$response | ConvertTo-Json -Depth 20