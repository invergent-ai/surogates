# frozen_string_literal: true

require "minitest/autorun"
require "yaml"

class ReleaseWorkflowTest < Minitest::Test
  def setup
    @workflow = YAML.load_file(".github/workflows/release.yml")
  end

  def test_docker_images_wait_for_npm_package_publication
    images_needs = Array(@workflow.fetch("jobs").fetch("images").fetch("needs", []))

    assert_includes images_needs, "npm"
  end
end
