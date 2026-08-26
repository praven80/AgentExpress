# Progress store the runtime writes to and the UI reads from.

resource "aws_dynamodb_table" "status" {
  name         = "${var.agent_name}_status"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "session_id"

  attribute {
    name = "session_id"
    type = "S"
  }
}

resource "aws_dynamodb_table" "events" {
  name         = "${var.agent_name}_events"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "session_id"
  range_key    = "ts"

  attribute {
    name = "session_id"
    type = "S"
  }
  attribute {
    name = "ts"
    type = "S"
  }
}
