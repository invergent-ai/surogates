# frozen_string_literal: true

require "minitest/autorun"
require "yaml"

class ReleaseWorkflowTest < Minitest::Test
  def setup
    @workflow = YAML.load_file(".github/workflows/release.yml")
  end

  def test_the_release_runs_for_a_pushed_version_tag_and_for_nothing_else
    # Ruby's YAML reads the key "on" as true. A pull request, a fork's among them, a push of a
    # branch and a run started by hand run none of the release's jobs: neither the one that holds
    # the desktop's release key, nor those that write to R2, GHCR or npm.
    refute @workflow.key?("on")
    assert_equal({ "push" => { "tags" => ["v*"] } }, @workflow.fetch(true))
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
    assert release_job.fetch("steps").any? { |step| step["uses"].to_s.start_with?("softprops/action-gh-release@") }

    jobs.except("release").each_value do |job|
      refute job.fetch("steps", []).any? { |step| step["uses"].to_s.start_with?("softprops/action-gh-release@") }
    end
  end

  def test_release_notes_are_generated_from_commit_messages
    steps = @workflow.fetch("jobs").fetch("release").fetch("steps")

    generate_step = steps.find { |step| step["name"] == "Generate release notes" }
    refute_nil generate_step, "Expected a release-notes generation step"
    assert_includes generate_step.fetch("run"), "scripts/release-notes.mjs"
    assert_path_exists "scripts/release-notes.mjs"

    release_step = steps.find { |step| step["uses"].to_s.start_with?("softprops/action-gh-release@") }
    assert_equal "release-notes.md", release_step.fetch("with").fetch("body_path")
    refute release_step.fetch("with").key?("generate_release_notes")
  end

  def test_the_release_uploads_the_wheel_and_the_sdist_alone
    release_step = @workflow.fetch("jobs").fetch("release").fetch("steps").find { |step| step["uses"].to_s.start_with?("softprops/action-gh-release@") }

    # Not whatever the build left in dist/: a desktop-vm-<key>.json there would be an anchor.
    assert_equal %w[dist/*.whl dist/*.tar.gz], release_step.fetch("with").fetch("files").split("\n")
  end

  def test_desktop_vm_image_is_published_once_per_key_after_the_kernel_check
    job = @workflow.fetch("jobs").fetch("desktop-vm-image")
    steps = job.fetch("steps")
    runs = steps.map { |step| step["run"].to_s }

    assert_equal({ "group" => "desktop-vm-image", "cancel-in-progress" => false }, job.fetch("concurrency"))
    assert_equal({ "contents" => "read" }, job.fetch("permissions"))
    assert_equal({ "state" => "${{ steps.published.outputs.state }}" }, job.fetch("outputs"))
    kernel = runs.index { |run| run.include?("images/guest/kernel-current.sh") }
    fetch = runs.index { |run| run.include?("images/guest/publish.sh fetch images/guest/out") }
    build = runs.index { |run| run.include?("images/guest/build.sh --packed images/guest/out") }
    send = runs.index { |run| run.include?("images/guest/publish.sh send images/guest/out") }
    refute_nil kernel
    assert_operator kernel, :<, fetch
    assert_operator fetch, :<, build
    assert_operator build, :<, send
    assert_equal "published", steps[fetch].fetch("id")
    [build, send].each do |index|
      assert_equal "steps.published.outputs.state == 'missing'", steps[index].fetch("if")
    end
    keep = steps.find { |step| step["uses"].to_s.start_with?("actions/upload-artifact@") }
    assert_equal "images/guest/out/manifest.json", keep.fetch("with").fetch("path")
    %w[images/guest/inputs.sh images/guest/build.sh images/guest/publish.sh images/guest/kernel-current.sh].each do |script|
      assert File.executable?(script), "#{script} is not executable"
    end
  end

  def test_only_the_jobs_that_write_a_release_may_write_its_assets
    jobs = @workflow.fetch("jobs")

    # A release's desktop-vm-<key>.json anchors the desktop's image: wheel builds with unpinned
    # dependencies and images runs third-party actions, and neither uploads a release asset.
    assert_equal({ "contents" => "read" }, jobs.fetch("wheel").fetch("permissions"))
    assert_equal({ "contents" => "read", "packages" => "write" }, jobs.fetch("images").fetch("permissions"))
  end

  def test_desktop_vm_jobs_are_bounded_past_the_wait_for_a_key_s_manifest
    jobs = @workflow.fetch("jobs")

    # publish.sh fetch waits up to 15 times 60 s for the release that sent a key to attach its manifest.
    assert_operator jobs.fetch("desktop-vm-image").fetch("timeout-minutes"), :>, 15
    assert jobs.fetch("desktop-vm-manifest").key?("timeout-minutes")
  end

  def test_desktop_vm_image_gives_the_r2_secrets_to_its_two_publish_steps_alone
    job = @workflow.fetch("jobs").fetch("desktop-vm-image")
    r2 = {
      "S3_ENDPOINT" => "${{ secrets.R2_ENDPOINT }}",
      "S3_BUCKET" => "${{ secrets.R2_BUCKET }}",
      "AWS_ACCESS_KEY_ID" => "${{ secrets.R2_ACCESS_KEY_ID }}",
      "AWS_SECRET_ACCESS_KEY" => "${{ secrets.R2_SECRET_ACCESS_KEY }}",
    }

    refute job.key?("env"), "the job's env reaches every step"
    job.fetch("steps").each do |step|
      run = step["run"].to_s
      if run.include?("images/guest/publish.sh fetch")
        assert_equal r2.merge("GH_TOKEN" => "${{ github.token }}"), step.fetch("env")
      elsif run.include?("images/guest/publish.sh send")
        assert_equal r2, step.fetch("env")
      else
        refute step.to_s.include?("secrets."), "#{step["name"] || step["uses"]} reads a secret"
      end
    end
  end

  def test_desktop_vm_manifest_is_attached_to_every_release_that_ships_its_key
    job = @workflow.fetch("jobs").fetch("desktop-vm-manifest")
    run = job.fetch("steps").map { |step| step["run"].to_s }.join("\n")

    assert_equal %w[release desktop-vm-image], Array(job.fetch("needs"))
    # Published by this release or checked against an earlier one's: a release deleted strands no key.
    refute job.key?("if")
    assert_includes run, "gh release view"
    assert_equal({ "contents" => "write" }, job.fetch("permissions"))
    download = job.fetch("steps").find { |step| step["uses"].to_s.start_with?("actions/download-artifact@") }
    assert_equal "desktop-vm-manifest", download.fetch("with").fetch("name")
    assert_includes run, 'desktop-vm-${key}.json'
    assert_includes run, 'gh release upload "$GITHUB_REF_NAME"'
  end

  def test_the_cloud_release_does_not_wait_for_the_desktop_image
    release_needs = Array(@workflow.fetch("jobs").fetch("release").fetch("needs", []))

    refute_includes release_needs, "desktop-vm-image"
    refute_includes release_needs, "desktop-vm-manifest"
  end

  def test_every_job_needs_only_jobs_of_the_workflow
    jobs = @workflow.fetch("jobs")

    jobs.each do |name, job|
      Array(job.fetch("needs", [])).each { |need| assert jobs.key?(need), "#{name} needs #{need}, which is no job of the release" }
    end
  end

  def test_desktop_build_makes_the_tarball_from_the_tag_with_the_vm_image_and_keeps_it
    job = @workflow.fetch("jobs").fetch("desktop-build")
    tarball = 'out/desktop/surogate-desktop-${GITHUB_REF_NAME#v}-linux-x64.tar.gz'

    assert_equal ["desktop-vm-image"], Array(job.fetch("needs"))
    assert_equal({ "contents" => "read" }, job.fetch("permissions"))
    assert_equal %w[needs outputs permissions runs-on steps timeout-minutes], job.keys.sort
    # What this job writes is what is signed, so the job is these steps, whole, and nothing between
    # them: the tag's own checkout, with no other ref named; the app built from it; the VM image's
    # manifest; the one packaging, with no fourth argument, which would name the install script
    # packed as the root helper and which only a test names; the tarball's hash and size, said
    # right after it is made; and the tarball kept. Each action by its commit.
    assert_equal [
      { "uses" => "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" },
      { "name" => "Set up Node", "uses" => "actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020", "with" => { "node-version" => "22" } },
      {
        "name" => "Build the app", "working-directory" => "desktop", "env" => { "NPM_CONFIG_USERCONFIG" => "/dev/null" },
        "run" => "# The app's node is fetched and checked against its pin, never one a runner kept.\nrm -rf bin\nnpm ci\nnode node_modules/electron/install.js\nnpm run build\n",
      },
      {
        "name" => "Take the VM image's manifest", "uses" => "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093",
        "with" => { "name" => "desktop-vm-manifest", "path" => "out/vm" },
      },
      # The agent's disk is made under fakeroot, which the job installs before it packages.
      { "name" => "Install fakeroot, which the agent's disk is made with", "run" => "sudo apt-get update -qq && sudo apt-get install -y -qq --no-install-recommends fakeroot" },
      { "name" => "Package the app", "run" => 'desktop/scripts/package.sh "${GITHUB_REF_NAME#v}" out/vm/manifest.json out/desktop' },
      {
        "name" => "Say the tarball's hash and size to the publish job", "id" => "tarball",
        "run" => "echo \"sha256=$(sha256sum <\"#{tarball}\" | cut -d' ' -f1)\" >>\"$GITHUB_OUTPUT\"\necho \"size=$(stat -c %s \"#{tarball}\")\" >>\"$GITHUB_OUTPUT\"\n",
      },
      {
        "name" => "Keep the tarball for the publish job", "uses" => "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
        "with" => { "name" => "desktop-tarball", "path" => "out/desktop/surogate-desktop-*-linux-x64.tar.gz", "if-no-files-found" => "error" },
      },
    ], job.fetch("steps")
    # The tarball's hash and its size are the job's own outputs, which no other job of the run can
    # set: an artifact is the run's, and any of its jobs may put another file under the tarball's
    # name. The signing opens no tarball, and has these two words for what it signs.
    assert_equal({ "sha256" => "${{ steps.tarball.outputs.sha256 }}", "size" => "${{ steps.tarball.outputs.size }}" }, job.fetch("outputs"))
  end

  def test_desktop_describe_reads_the_built_tarball_in_a_job_that_holds_no_secret_and_says_one_word_of_it
    job = @workflow.fetch("jobs").fetch("desktop-describe")
    steps = job.fetch("steps")

    # All that reads the build's tarball, on a runner of its own: a step of the job that signs
    # could write into that job's checkout, or leave something running beside its key. The job
    # is these three steps, whole, each action by its commit (v4.4.0, v4.3.0).
    assert_equal [
      { "uses" => "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" },
      { "name" => "Take the tarball", "uses" => "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093", "with" => { "name" => "desktop-tarball", "path" => "out/desktop" } },
      {
        "name" => "Write its manifest, and say the app's state schema to the publish job",
        "id" => "manifest",
        # The hash the build's job gave for its tarball, through the step's environment, never
        # pasted into its script, where what the build says would be run.
        "env" => { "DESKTOP_TARBALL_SHA256" => "${{ needs.desktop-build.outputs.sha256 }}" },
        "run" => "desktop/release/publish.sh describe \"${GITHUB_REF_NAME#v}\" out/desktop\necho \"schema=$(jq -r .stateSchema out/desktop/manifest.json)\" >>\"$GITHUB_OUTPUT\"\n",
      },
    ], steps
    # One word is handed on, as the job's own output, which no other job can set: the state schema.
    # Nothing of this job's is an artifact, which is the run's.
    assert_equal({ "schema" => "${{ steps.manifest.outputs.schema }}" }, job.fetch("outputs"))
    # No secret by any name, no Environment, which would hand it every secret of that one, and no
    # env of the job's own; a runner of its own; and the build before it, whose hash it checks.
    assert_equal %w[needs outputs permissions runs-on steps timeout-minutes], job.keys.sort
    assert_equal ["desktop-build"], Array(job.fetch("needs"))
    assert_equal({ "contents" => "read" }, job.fetch("permissions"))
    assert_equal "blacksmith-4vcpu-ubuntu-2404", job.fetch("runs-on")
    refute_match(/secrets/i, job.to_s)
    refute_match(/DESKTOP_RELEASE_KEY/i, job.to_s)
    refute_match(/desktop-release/i, job.to_s)
  end

  def test_desktop_publish_signs_and_sends_the_built_tarball_one_release_at_a_time_in_its_environment
    job = @workflow.fetch("jobs").fetch("desktop-publish")
    steps = job.fetch("steps")
    runs = steps.map { |step| step["run"].to_s }

    # Both: the build, for its two words of the tarball, and the job that read it, for its one.
    assert_equal %w[desktop-build desktop-describe], Array(job.fetch("needs"))
    assert_equal "desktop-release", job.fetch("environment")
    assert_equal({ "contents" => "read" }, job.fetch("permissions"))
    assert_equal({ "group" => "desktop-release", "cancel-in-progress" => false }, job.fetch("concurrency"))
    taken = steps.each_index.select { |index| steps[index]["uses"].to_s.start_with?("actions/download-artifact@") }
    # The tarball alone is taken, to be sent: the manifest is written here, and no other job's file is read for it.
    assert_equal [{ "name" => "desktop-tarball", "path" => "out/desktop" }], taken.map { |index| steps[index].fetch("with") }
    sign = runs.index { |run| run.include?('desktop/release/publish.sh sign "${GITHUB_REF_NAME#v}" out/desktop') }
    send = runs.index { |run| run.include?('desktop/release/publish.sh send "${GITHUB_REF_NAME#v}" out/desktop') }
    refute_nil sign
    refute_nil send
    # The tarball is here before its manifest is signed, and that is signed before both are sent.
    assert_operator taken.max, :<, sign
    assert_operator sign, :<, send
    # The job that holds the key reads no tarball: it writes no manifest of one, and unpacks none.
    runs.each do |run|
      refute_match(/\bdescribe\b/, run, "the job that holds the release key writes a tarball's manifest")
      refute_match(/\b(tar|unzip|gzip|gunzip|zcat)\b/, run, "the job that holds the release key unpacks an archive")
    end
    %w[desktop/scripts/package.sh desktop/release/publish.sh desktop/release/install.sh].each do |script|
      assert File.executable?(script), "#{script} is not executable"
    end
  end

  def test_only_the_desktop_s_publish_job_holds_its_secrets_and_it_runs_no_npm
    jobs = @workflow.fetch("jobs")
    r2 = {
      "S3_ENDPOINT" => "${{ secrets.R2_ENDPOINT }}",
      "S3_BUCKET" => "${{ secrets.R2_BUCKET }}",
      "AWS_ACCESS_KEY_ID" => "${{ secrets.R2_ACCESS_KEY_ID }}",
      "AWS_SECRET_ACCESS_KEY" => "${{ secrets.R2_SECRET_ACCESS_KEY }}",
    }

    # npm ci runs the dependency tree's install scripts, and a job's steps share its runner: the
    # build holds no secret, and the job that holds them installs and runs nothing of npm's.
    %w[desktop-build desktop-describe desktop-publish].each { |name| refute jobs.fetch(name).key?("env"), "#{name}'s env reaches every step" }
    %w[desktop-build desktop-describe].each do |name|
      jobs.fetch(name).fetch("steps").each do |step|
        refute step.to_s.include?("secrets."), "#{name}'s #{step["name"] || step["uses"]} reads a secret"
      end
    end
    jobs.fetch("desktop-publish").fetch("steps").each do |step|
      run = step["run"].to_s
      refute_match(/\b(npm|npx|node)\b/, run, "#{step["name"] || step["uses"]} runs npm or node")
      refute_includes step["uses"].to_s, "setup-node"
      if run.include?("publish.sh sign")
        # With the key, three words that are each a job's own output: the build's for its tarball,
        # its hash and its size, and the describe job's for the app's state schema. The signing
        # opens no tarball, and reads no artifact.
        assert_equal({
          "DESKTOP_RELEASE_KEY" => "${{ secrets.DESKTOP_RELEASE_KEY }}",
          "DESKTOP_TARBALL_SHA256" => "${{ needs.desktop-build.outputs.sha256 }}",
          "DESKTOP_TARBALL_SIZE" => "${{ needs.desktop-build.outputs.size }}",
          "DESKTOP_STATE_SCHEMA" => "${{ needs.desktop-describe.outputs.schema }}",
        }, step.fetch("env"))
      elsif run.include?("publish.sh send")
        assert_equal r2, step.fetch("env")
      else
        refute step.to_s.include?("secrets."), "#{step["name"] || step["uses"]} reads a secret"
      end
    end
    jobs.except("desktop-publish").each { |name, job| refute job.to_s.include?("DESKTOP_RELEASE_KEY"), "#{name} reads the release key" }
    # The release key is in one step's environment, the signing's, and nowhere wider: not the
    # job's, which the check above refuses, and not the workflow's.
    keyed = jobs.fetch("desktop-publish").fetch("steps").select { |step| step.to_s.include?("DESKTOP_RELEASE_KEY") }
    assert_equal ['desktop/release/publish.sh sign "${GITHUB_REF_NAME#v}" out/desktop'], keyed.map { |step| step["run"].to_s.strip }
    refute @workflow.except("jobs").to_s.include?("secrets."), "the workflow gives a secret to every job"
  end

  def test_no_workflow_turns_off_its_runner_s_restriction_of_user_namespaces
    # A stock Ubuntu 24.04 refuses a user namespace to a program without a profile of its own. No
    # job needs one, and none changes a kernel setting of its runner's to get one.
    workflows = Dir[".github/workflows/*.yml"]
    assert_includes workflows, ".github/workflows/release.yml"
    workflows.each do |workflow|
      refute_match(/apparmor_restrict_unprivileged_userns|\bsysctl\b|\bunshare\b/, File.read(workflow), workflow)
    end
  end

  def test_desktop_jobs_are_bounded
    jobs = @workflow.fetch("jobs")

    assert jobs.fetch("desktop-build").key?("timeout-minutes")
    assert jobs.fetch("desktop-describe").key?("timeout-minutes")
    assert jobs.fetch("desktop-publish").key?("timeout-minutes")
  end

  def test_the_cloud_release_does_not_wait_for_the_desktop
    release_needs = Array(@workflow.fetch("jobs").fetch("release").fetch("needs", []))

    refute_includes release_needs, "desktop-build"
    refute_includes release_needs, "desktop-describe"
    refute_includes release_needs, "desktop-publish"
  end

  def test_desktop_publish_is_the_tag_s_checkout_the_tarball_s_download_and_publish_sh_and_nothing_else
    job = @workflow.fetch("jobs").fetch("desktop-publish")
    steps = job.fetch("steps")

    # A step need not name npm to run it: package.sh does, and so may an action, a container's
    # image, or a runner that kept what an earlier job's npm left on it. The job that holds the
    # keys is these four steps, each with these keys alone, on a runner of its own.
    # Its two actions are named by their commits (v4.4.0 and v4.3.0): a tag can be moved to other
    # code, and the job that holds the release key would run it.
    assert_equal [
      { "uses" => "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" },
      { "uses" => "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093" },
      { "run" => 'desktop/release/publish.sh sign "${GITHUB_REF_NAME#v}" out/desktop' },
      { "run" => 'desktop/release/publish.sh send "${GITHUB_REF_NAME#v}" out/desktop' },
    ], steps.map { |step| step.slice("uses", "run") }
    # The checkout names no ref, repository or path: publish.sh and install.sh are the tag's.
    assert_equal [%w[uses], %w[name uses with], %w[env name run], %w[env name run]], steps.map { |step| step.keys.sort }
    assert_equal %w[concurrency environment needs permissions runs-on steps timeout-minutes], job.keys.sort
    assert_equal "blacksmith-4vcpu-ubuntu-2404", job.fetch("runs-on")
  end

  def test_the_desktop_s_secrets_reach_no_other_job_however_they_are_named
    jobs = @workflow.fetch("jobs")

    # An Environment's secrets reach every job that names it, all of them at once through
    # toJSON(secrets), which names none.
    jobs.except("desktop-publish").each do |name, job|
      refute_includes job["environment"].to_s, "desktop-release", "#{name} runs in the desktop's Environment"
    end
    # secrets.X, secrets['X'] or all of them: the build names no secret in any form.
    refute_match(/\bsecrets\b/, jobs.fetch("desktop-build").to_s)
    # R2's keys can rewrite desktop/install.sh, which runs as root on each new install: the guest
    # image's job and the desktop's publish job read them, and no other.
    r2 = /\bR2_(ENDPOINT|BUCKET|ACCESS_KEY_ID|SECRET_ACCESS_KEY)\b/
    assert_equal %w[desktop-vm-image desktop-publish], jobs.select { |_, job| job.to_s.match?(r2) }.keys
  end

  def test_the_workflow_has_no_key_of_its_own_but_its_name_its_trigger_its_permissions_and_its_jobs
    # A shell, an environment or a concurrency of the workflow's own is every job's: a shell that
    # traces would print what a step reads, an env would be every step's, the release key's step
    # among them, and a concurrency that cancels would cut a send short.
    assert_equal ["jobs", "name", "permissions", "true"], @workflow.keys.map(&:to_s).sort
    assert_equal({ "contents" => "write", "packages" => "write" }, @workflow.fetch("permissions"))
  end

  # Each workflow file of the repository, read: a second file is started by its own trigger, and
  # reads the repository's secrets as this one does.
  def workflows
    Dir[".github/workflows/*"].sort.to_h { |file| [File.basename(file), YAML.load_file(file)] }
  end

  def test_every_secret_is_read_by_its_own_name_in_its_own_step_in_every_workflow_file
    r2 = %w[R2_ENDPOINT R2_BUCKET R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY]
    # Who reads which secret: each file, each job, each step. R2's keys can rewrite
    # desktop/install.sh, which runs as root on each new install; the desktop's release key signs
    # what every installed app takes. A reader that is not here is one too many.
    allowed = {
      "release.yml" => {
        ["images", "Log in to GHCR"] => %w[GITHUB_TOKEN],
        ["npm", "Publish SDK packages"] => %w[NPM_TOKEN],
        ["desktop-vm-image", "Look for the image's key in our releases, and check R2 against it"] => r2,
        ["desktop-vm-image", "Publish the image"] => r2,
        ["desktop-publish", "Sign its manifest"] => %w[DESKTOP_RELEASE_KEY],
        ["desktop-publish", "Publish the release"] => r2,
      },
      "update-images.yml" => {
        ["update-agent-images", nil] => %w[NODE_HOSTS MASTER_HOST],
        ["update-agent-images", "Write SSH identity file"] => %w[SSH_KEY],
      },
    }
    read = workflows.to_h do |file, workflow|
      # Named with whatever letters, large or small: GitHub reads both the word and the name so.
      named = ->(part) { part.to_s.scan(/secrets\s*\.\s*([A-Za-z0-9_]+)/i).flatten }
      found = {}
      found[[nil, nil]] = named.call(workflow.reject { |key, _| key == "jobs" })
      workflow.fetch("jobs").each do |name, job|
        found[[name, nil]] = named.call(job.reject { |key, _| key == "steps" })
        job.fetch("steps", []).each { |step| (found[[name, step["name"] || step["uses"]]] ||= []).concat(named.call(step)) }
      end
      [file, found.reject { |_, names| names.empty? }]
    end
    assert_equal allowed, read
    # And by no other way than its name: not by brackets, not all of them at once, and not handed
    # on whole to a workflow that is called. Every use of the word is the word, a dot and a name.
    Dir[".github/workflows/*"].sort.each do |file|
      text = File.read(file)
      assert_equal text.scan(/secrets/i).length, text.scan(/secrets\.[A-Za-z0-9_]+/).length, "#{file} reads a secret by another way than its name"
      refute_match(/\binherit\b/, text, file)
    end
  end

  def test_the_cluster_s_update_runs_by_hand_or_after_this_repository_s_own_release_of_a_tag_and_after_no_other_run
    # A workflow_run names a workflow by its name alone, and any workflow file may take that
    # name: one a pull request adds, a fork's among them, or one pushed on a branch. This file
    # then runs from the default branch, with the key to every node. So its one job asks the run
    # that ended what makes it the release: that it ended well, that a push started it, that it
    # ran this repository's own code, from the release's own file, for a version's tag.
    workflow = workflows.fetch("update-images.yml")
    assert_equal({ "workflow_run" => { "workflows" => ["Release"], "types" => ["completed"] }, "workflow_dispatch" => nil }, workflow.fetch(true))
    assert_equal "Release", @workflow.fetch("name")
    assert_equal ["update-agent-images"], workflow.fetch("jobs").keys
    run = "github.event.workflow_run"
    asked = [
      "#{run}.conclusion == 'success'", "#{run}.event == 'push'", "#{run}.head_repository.full_name == github.repository",
      "#{run}.path == '.github/workflows/release.yml'", "startsWith(#{run}.head_branch, 'v')",
    ]
    assert_equal "${{ github.event_name == 'workflow_dispatch' || (#{asked.join(" && ")}) }}", workflow.fetch("jobs").fetch("update-agent-images").fetch("if")
    assert File.exist?(".github/workflows/release.yml")
  end

  def test_one_job_alone_runs_in_the_desktop_s_environment_in_every_workflow_file
    # An Environment's secrets reach every job that names it, whatever letters it is named with.
    named = workflows.flat_map do |file, workflow|
      workflow.fetch("jobs").select { |_, job| job.key?("environment") }.map { |name, job| [file, name, job["environment"]] }
    end
    assert_equal [["release.yml", "desktop-publish", "desktop-release"]], named
  end

  def test_every_action_is_named_by_its_commit_in_every_workflow_file
    # A tag or a branch can be moved to other code: the build's job writes what is signed, other
    # jobs hold R2's keys beside the actions they run, and the second file's job holds the key to
    # every node of the cluster. So every file is asked, each step of each job and each job that
    # is another workflow, and a file that runs no action today is held to it for the day it does.
    by_commit = %r{\A[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}\z}
    run = Hash.new { |files, file| files[file] = [] }
    workflows.each do |file, workflow|
      run[file]
      workflow.fetch("jobs").each do |name, job|
        named = [job["uses"]] + job.fetch("steps", []).map { |step| step["uses"] }
        named.compact.each do |action|
          assert_match(by_commit, action, "#{file}: #{name} runs an action that is not named by its commit")
          run[file] << action.split("@").first
        end
      end
    end
    assert_equal %w[release.yml update-images.yml], run.keys
    assert_equal %w[
      actions/checkout actions/download-artifact actions/setup-node actions/upload-artifact astral-sh/setup-uv docker/build-push-action
      docker/login-action docker/setup-buildx-action pnpm/action-setup softprops/action-gh-release
    ], run.fetch("release.yml").uniq.sort
    assert_empty run.fetch("update-images.yml"), "update-images.yml runs an action: name it here, by its commit in the file"
  end

  def test_the_look_for_an_action_reads_the_file_as_the_runner_does
    # Every "uses" of a workflow file's text is one the test above has asked: none is in a place it
    # does not look, as a step of a list that a key of another name holds would be.
    workflows.each do |file, workflow|
      read = workflow.fetch("jobs").sum { |_, job| (job.key?("uses") ? 1 : 0) + job.fetch("steps", []).count { |step| step.key?("uses") } }
      assert_equal File.read(".github/workflows/#{file}").scan(/^\s*(?:-\s+)?uses:/).length, read, "#{file} names an action where no job's steps are"
    end
  end

  def test_each_artifact_is_one_job_s_to_keep_and_is_never_overwritten
    # An artifact is the run's: any job may put a file under its name. The desktop's tarball is the
    # build's alone to keep, and no step may replace what another kept.
    kept = []
    taken = []
    @workflow.fetch("jobs").each do |name, job|
      job.fetch("steps").each do |step|
        uses = step["uses"].to_s
        kept << [name, step.fetch("with").fetch("name")] if uses.start_with?("actions/upload-artifact@")
        taken << [name, step.fetch("with").fetch("name")] if uses.start_with?("actions/download-artifact@")
        refute step.fetch("with", {}).key?("overwrite"), "#{name} overwrites an artifact"
        refute_match(/upload-artifact|download-artifact|gh run download|actions\/artifacts/, step["run"].to_s, "#{name} keeps or takes an artifact by hand")
      end
    end
    assert_equal [%w[wheel dist], %w[desktop-vm-image desktop-vm-manifest], %w[desktop-build desktop-tarball]], kept
    assert_equal [
      %w[desktop-vm-manifest desktop-vm-manifest], %w[desktop-build desktop-vm-manifest], %w[desktop-describe desktop-tarball],
      %w[desktop-publish desktop-tarball], %w[release dist],
    ], taken
  end
end
