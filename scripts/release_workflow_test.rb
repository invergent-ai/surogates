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

  def test_docker_image_matrix_includes_browser_image
    image_entries = @workflow
      .fetch("jobs")
      .fetch("images")
      .fetch("strategy")
      .fetch("matrix")
      .fetch("include")

    assert_includes image_entries, {
      "dir" => "browser",
      "name" => "surogates-agent-browser",
    }
    assert_path_exists "images/browser/Dockerfile"
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

  def test_release_notes_are_generated_from_commit_messages
    steps = @workflow.fetch("jobs").fetch("release").fetch("steps")

    generate_step = steps.find { |step| step["name"] == "Generate release notes" }
    refute_nil generate_step, "Expected a release-notes generation step"
    assert_includes generate_step.fetch("run"), "scripts/release-notes.mjs"
    assert_path_exists "scripts/release-notes.mjs"

    release_step = steps.find { |step| step["uses"] == "softprops/action-gh-release@v2" }
    assert_equal "release-notes.md", release_step.fetch("with").fetch("body_path")
    refute release_step.fetch("with").key?("generate_release_notes")
  end
end
