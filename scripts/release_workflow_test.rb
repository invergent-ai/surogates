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

  def test_npm_packages_publish_with_release_tag_version
    publish_step = @workflow
      .fetch("jobs")
      .fetch("npm")
      .fetch("steps")
      .find { |step| step["name"] == "Publish SDK packages" }

    assert_includes publish_step.fetch("run"), '--version="${GITHUB_REF_NAME#v}"'
  end

  def test_github_release_waits_for_all_release_tasks
    jobs = @workflow.fetch("jobs")
    release_job = jobs.fetch("release")
    release_needs = Array(release_job.fetch("needs", []))

    assert_includes release_needs, "wheel"
    assert_includes release_needs, "images"
    assert release_job.fetch("steps").any? { |step| step["uses"] == "softprops/action-gh-release@v2" }

    jobs.except("release").each_value do |job|
      refute job.fetch("steps", []).any? { |step| step["uses"] == "softprops/action-gh-release@v2" }
    end
  end
end
