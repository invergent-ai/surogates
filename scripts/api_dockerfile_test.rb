# frozen_string_literal: true

require "minitest/autorun"

class ApiDockerfileTest < Minitest::Test
  def setup
    @dockerfile = File.read("images/api/Dockerfile")
  end

  def test_web_build_stage_copies_agent_chat_package_before_installing_web_dependencies
    assert_order "COPY sdk/agent-chat-react/package.json", "RUN npm ci"
  end

  def test_web_build_stage_copies_agent_chat_source_before_building_web
    assert_order "COPY sdk/agent-chat-react/", "RUN npx vite build"
  end

  def test_web_build_stage_resolves_agent_chat_source_dependencies_from_web_install
    assert_order "ln -s /build/node_modules /sdk/agent-chat-react/node_modules", "RUN npx vite build"
  end

  private

  def assert_order(first, second)
    first_index = @dockerfile.index(first)
    second_index = @dockerfile.index(second)

    refute_nil first_index, "Expected Dockerfile to include #{first.inspect}"
    refute_nil second_index, "Expected Dockerfile to include #{second.inspect}"
    assert_operator first_index, :<, second_index
  end
end
